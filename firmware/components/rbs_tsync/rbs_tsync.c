/* rbs_tsync — reference-broadcast BLE time-sync. See rbs_tsync.h. */
#include "rbs_tsync.h"

#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_random.h"
#include "esp_heap_caps.h"
#include "cJSON.h"

#include "host/ble_hs.h"
#include "host/ble_gap.h"

static const char *TAG = "rbs_tsync";

#define TSYNC_RING       96
#define REPORT_PERIOD_MS 5000
#define RESTAMP_MS       1000     /* < the 1.5 s adv interval → fresh tx_us per emission */
#define TOPIC_PREFIX     "rbs"

/* ── identity + callbacks ─────────────────────────────────────────────── */
static char                 s_letter;
static char                 s_node_id[40];
static char                 s_topic[64];
static uint8_t              s_boot_nonce;
static uint8_t              s_counter;
static bool                 s_started;
static rbs_tsync_publish_cb s_publish;
static rbs_tsync_ready_cb   s_ready;

/* ── RX ring (PSRAM-backed; no-op if alloc fails) ─────────────────────── */
static rbs_tsync_evt_t *s_ring;
static int              s_head, s_count;
static portMUX_TYPE     s_mux = portMUX_INITIALIZER_UNLOCKED;

static void ring_push(const rbs_tsync_evt_t *e)
{
    if (!s_ring) return;
    portENTER_CRITICAL(&s_mux);
    s_ring[s_head] = *e;
    s_head = (s_head + 1) % TSYNC_RING;
    if (s_count < TSYNC_RING) s_count++;
    portEXIT_CRITICAL(&s_mux);
}

int rbs_tsync_drain(rbs_tsync_evt_t *out, int max)
{
    if (!s_ring) return 0;
    portENTER_CRITICAL(&s_mux);
    int n = s_count < max ? s_count : max;
    int start = (s_head - s_count + TSYNC_RING) % TSYNC_RING;
    for (int i = 0; i < n; i++)
        out[i] = s_ring[(start + i) % TSYNC_RING];
    s_count -= n;
    portEXIT_CRITICAL(&s_mux);
    return n;
}

uint8_t rbs_tsync_boot_nonce(void) { return s_boot_nonce; }

/* ── TX: announce payload (22-byte 0xFFFE mfg field) ──────────────────── */
/* bytes: [0]=0xFE [1]=0xFF (company 0xFFFE LE), [2..3]="BT", [4]=letter,
 *        [5]=counter, [6]=tx_dbm, [7..12]=reserved/diagnostic (0 here),
 *        [13..20]=tx_us (64-bit LE), [21]=boot_nonce. */
static int announce_apply(void)
{
    uint8_t mfg[22] = {0};
    mfg[0] = 0xFE; mfg[1] = 0xFF;
    mfg[2] = 'B';  mfg[3] = 'T';
    mfg[4] = (uint8_t)s_letter;
    mfg[5] = s_counter++;
    mfg[6] = (uint8_t)(int8_t)9;             /* nominal ESP32-S3 BLE TX dBm */
    uint64_t tx_us = (uint64_t)esp_timer_get_time();
    for (int b = 0; b < 8; b++)
        mfg[13 + b] = (uint8_t)((tx_us >> (8 * b)) & 0xFF);
    mfg[21] = s_boot_nonce;

    struct ble_hs_adv_fields fields = {0};
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.mfg_data = mfg;
    fields.mfg_data_len = sizeof(mfg);
    return ble_gap_adv_set_fields(&fields);   /* live-updates while advertising */
}

static void restamp_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(RESTAMP_MS));
        if (s_started) announce_apply();
    }
}

static int announce_gap_event(struct ble_gap_event *event, void *arg)
{
    (void)event; (void)arg;
    return 0;                                  /* non-connectable; nothing to do */
}

/* ── RX: passive scan GAP handler ─────────────────────────────────────── */
static int scan_gap_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    if (event->type != BLE_GAP_EVENT_DISC) return 0;
    /* Stamp RX first — before any parsing — to minimise one-sided callback delay. */
    int64_t rx_us = esp_timer_get_time();

    struct ble_gap_disc_desc *desc = &event->disc;
    struct ble_hs_adv_fields fields;
    if (ble_hs_adv_parse_fields(&fields, desc->data, desc->length_data) != 0)
        return 0;
    if (fields.mfg_data_len < 7) return 0;

    uint16_t company = fields.mfg_data[0] | (fields.mfg_data[1] << 8);
    if (company != 0xFFFE || fields.mfg_data[2] != 'B' || fields.mfg_data[3] != 'T')
        return 0;

    uint64_t tx_us = 0;
    uint8_t  tx_boot = 0;
    if (fields.mfg_data_len >= 22) {
        for (int b = 0; b < 8; b++)
            tx_us |= (uint64_t)fields.mfg_data[13 + b] << (8 * b);
        tx_boot = fields.mfg_data[21];
    }
    rbs_tsync_evt_t ev = {
        .rx_us = rx_us, .tx_us = tx_us,
        .tx_letter = (char)fields.mfg_data[4],
        .tx_boot = tx_boot, .rssi = desc->rssi,
    };
    ring_push(&ev);
    return 0;
}

static esp_err_t start_scan(void)
{
    struct ble_gap_disc_params p = {
        .itvl = 160,                  /* 100 ms */
        .window = 160,                /* 100 ms — 100% duty cycle, never idle */
        .passive = 1,                 /* no SCAN_REQ, just listen */
        .filter_duplicates = 0,       /* report every advertisement */
        .limited = 0,
    };
    int rc = ble_gap_disc(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, &p, scan_gap_event, NULL);
    if (rc != 0) ESP_LOGE(TAG, "ble_gap_disc failed: %d", rc);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

static esp_err_t start_announce(void)
{
    if (announce_apply() != 0) return ESP_FAIL;
    struct ble_gap_adv_params adv = {
        .conn_mode = BLE_GAP_CONN_MODE_NON,
        .disc_mode = BLE_GAP_DISC_MODE_GEN,
        .itvl_min = 2400,             /* 1.5 s (units of 0.625 ms) */
        .itvl_max = 2560,             /* 1.6 s */
    };
    uint8_t own_addr_type = 0;
    ble_hs_id_infer_auto(0, &own_addr_type);
    int rc = ble_gap_adv_start(own_addr_type, NULL, BLE_HS_FOREVER, &adv,
                               announce_gap_event, NULL);
    if (rc != 0) { ESP_LOGE(TAG, "adv_start failed: %d", rc); return ESP_FAIL; }
    return ESP_OK;
}

/* ── reporter: drain ring → JSON → publish callback ───────────────────── */
static void report_task(void *arg)
{
    (void)arg;
    rbs_tsync_evt_t *evs = heap_caps_malloc(TSYNC_RING * sizeof(*evs), MALLOC_CAP_SPIRAM);
    if (!evs) evs = malloc(TSYNC_RING * sizeof(*evs));
    if (!evs) { ESP_LOGE(TAG, "report buf alloc failed"); vTaskDelete(NULL); return; }

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(REPORT_PERIOD_MS));
        if (!s_publish) continue;
        if (s_ready && !s_ready()) continue;       /* transport down — keep buffering */
        int n = rbs_tsync_drain(evs, TSYNC_RING);
        if (n <= 0) continue;

        cJSON *root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "node", s_node_id);
        cJSON_AddNumberToObject(root, "boot", s_boot_nonce);
        cJSON_AddNumberToObject(root, "n", n);
        cJSON *arr = cJSON_AddArrayToObject(root, "e");
        for (int i = 0; i < n; i++) {
            cJSON *a = cJSON_CreateArray();
            char tl[2] = { evs[i].tx_letter, 0 };
            cJSON_AddItemToArray(a, cJSON_CreateString(tl));
            cJSON_AddItemToArray(a, cJSON_CreateNumber(evs[i].tx_boot));
            cJSON_AddItemToArray(a, cJSON_CreateNumber((double)evs[i].tx_us));
            cJSON_AddItemToArray(a, cJSON_CreateNumber((double)evs[i].rx_us));
            cJSON_AddItemToArray(a, cJSON_CreateNumber(evs[i].rssi));
            cJSON_AddItemToArray(arr, a);
        }
        char *json = cJSON_PrintUnformatted(root);
        if (json) { s_publish(s_topic, json, (int)strlen(json)); cJSON_free(json); }
        cJSON_Delete(root);
    }
}

/* ── public init ──────────────────────────────────────────────────────── */
esp_err_t rbs_tsync_init(char node_letter, const char *node_id,
                         rbs_tsync_publish_cb publish, rbs_tsync_ready_cb ready)
{
    s_letter = node_letter;
    snprintf(s_node_id, sizeof(s_node_id), "%s", node_id ? node_id : "node");
    snprintf(s_topic, sizeof(s_topic), "%s/tsync_rx/%s", TOPIC_PREFIX, s_node_id);
    s_publish = publish;
    s_ready = ready;
    s_boot_nonce = (uint8_t)(esp_random() & 0xFF);

    s_ring = heap_caps_malloc(TSYNC_RING * sizeof(*s_ring), MALLOC_CAP_SPIRAM);
    if (!s_ring) s_ring = malloc(TSYNC_RING * sizeof(*s_ring));
    if (!s_ring) ESP_LOGW(TAG, "ring alloc failed — RX disabled");

    if (start_announce() != ESP_OK) return ESP_FAIL;
    if (start_scan() != ESP_OK)     return ESP_FAIL;
    s_started = true;

    if (xTaskCreatePinnedToCoreWithCaps(restamp_task, "rbs_restamp", 3072, NULL, 4,
                                        NULL, tskNO_AFFINITY, MALLOC_CAP_SPIRAM) != pdPASS)
        xTaskCreate(restamp_task, "rbs_restamp", 3072, NULL, 4, NULL);
    if (xTaskCreatePinnedToCoreWithCaps(report_task, "rbs_report", 4096, NULL, 4,
                                        NULL, tskNO_AFFINITY, MALLOC_CAP_SPIRAM) != pdPASS)
        xTaskCreate(report_task, "rbs_report", 4096, NULL, 4, NULL);

    ESP_LOGI(TAG, "rbs_tsync up: node '%c' (%s), boot %u, topic %s",
             node_letter, s_node_id, s_boot_nonce, s_topic);
    return ESP_OK;
}
