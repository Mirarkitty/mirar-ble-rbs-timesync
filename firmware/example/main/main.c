/* rbs_tsync example: announce + scan + publish tsync_rx reports.
 *
 * If CONFIG_EXAMPLE_WIFI_SSID is set, connects WiFi + MQTT and publishes reports
 * to the broker (the path the Python server consumes). Otherwise it falls back to
 * printing each report to the UART console — flash two boards (different node
 * letters) and watch the JSON reports stream out, no broker required.
 */
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_event.h"

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"

#include "rbs_tsync.h"

static const char *TAG = "example";

#define NODE_LETTER (CONFIG_EXAMPLE_NODE_LETTER[0])
#define NODE_ID     (CONFIG_EXAMPLE_NODE_ID)

/* ── transport: MQTT if configured, else UART printf ──────────────────── */
#if CONFIG_EXAMPLE_USE_WIFI
#include "esp_wifi.h"
#include "esp_netif.h"
#include "mqtt_client.h"

static esp_mqtt_client_handle_t s_mqtt;
static volatile bool s_mqtt_up;

static void mqtt_pub_cb(const char *topic, const char *json, int len) {
    if (s_mqtt) esp_mqtt_client_publish(s_mqtt, topic, json, len, 0, 0);
}
static bool mqtt_ready_cb(void) { return s_mqtt_up; }

static void mqtt_ev(void *h, esp_event_base_t b, int32_t id, void *data) {
    if (id == MQTT_EVENT_CONNECTED)    s_mqtt_up = true;
    if (id == MQTT_EVENT_DISCONNECTED) s_mqtt_up = false;
}
static void wifi_ev(void *h, esp_event_base_t b, int32_t id, void *data) {
    if (b == WIFI_EVENT && id == WIFI_EVENT_STA_START) esp_wifi_connect();
    if (b == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) esp_wifi_connect();
}
static void net_start(void) {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t wc = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wc));
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_ev, NULL, NULL);
    wifi_config_t cfg = {0};
    strncpy((char *)cfg.sta.ssid, CONFIG_EXAMPLE_WIFI_SSID, sizeof(cfg.sta.ssid) - 1);
    strncpy((char *)cfg.sta.password, CONFIG_EXAMPLE_WIFI_PASSWORD, sizeof(cfg.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    esp_mqtt_client_config_t mc = { .broker.address.uri = CONFIG_EXAMPLE_MQTT_URI };
    s_mqtt = esp_mqtt_client_init(&mc);
    esp_mqtt_client_register_event(s_mqtt, ESP_EVENT_ANY_ID, mqtt_ev, NULL);
    esp_mqtt_client_start(s_mqtt);
}
#define PUBLISH_CB mqtt_pub_cb
#define READY_CB   mqtt_ready_cb
#else  /* no WiFi configured → print reports to UART */
static void uart_pub_cb(const char *topic, const char *json, int len) {
    printf("%s %.*s\n", topic, len, json);
}
#define PUBLISH_CB uart_pub_cb
#define READY_CB   NULL
static void net_start(void) { ESP_LOGW(TAG, "no WiFi configured — reports go to UART"); }
#endif

/* ── NimBLE host ──────────────────────────────────────────────────────── */
static void on_sync(void) {
    ESP_LOGI(TAG, "BLE synced; starting rbs_tsync as '%c' (%s)", NODE_LETTER, NODE_ID);
    rbs_tsync_init(NODE_LETTER, NODE_ID, PUBLISH_CB, READY_CB);
}
static void host_task(void *arg) { nimble_port_run(); nimble_port_freertos_deinit(); }

void app_main(void) {
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase(); nvs_flash_init();
    }
    net_start();

    nimble_port_init();
    ble_hs_cfg.sync_cb = on_sync;
    nimble_port_freertos_init(host_task);
}
