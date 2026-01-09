#include "esp_camera.h"
#include <WiFi.h>
#include <WiFiManager.h>  // Install via Library Manager: "WiFiManager" by tzapu
#include <Wire.h>
#include <LiquidCrystal_I2C.h> // Install via Library Manager: "LiquidCrystal I2C" by Frank de Brabander
#include "esp_http_server.h"

// ==========================================
// 1. PIN DEFINITIONS
// ==========================================

// CAMERA PINS (AI THINKER Model)
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// LCD I2C PINS
// Note: These pins conflict with the SD Card slot.
#define I2C_SDA 15
#define I2C_SCL 14
#define LCD_ADDR 0x27  // Check your address (usually 0x27 or 0x3F)

LiquidCrystal_I2C lcd(LCD_ADDR, 16, 2);

// SERVER HANDLES
httpd_handle_t stream_httpd = NULL;
httpd_handle_t camera_httpd = NULL;

// ==========================================
// 2. HELPER FUNCTIONS
// ==========================================

// Decodes URL strings (converts %20 to space, etc.)
void urlDecode(char *dst, const char *src) {
  char a, b;
  while (*src) {
    if ((*src == '%') &&
        ((a = src[1]) && (b = src[2])) &&
        (isxdigit(a) && isxdigit(b))) {
      if (a >= 'a') a -= 'a' - 'A';
      if (a >= 'A') a -= 'A' - 10;
      else a -= '0';
      if (b >= 'a') b -= 'a' - 'A';
      if (b >= 'A') b -= 'A' - 10;
      else b -= '0';
      *dst++ = 16 * a + b;
      src += 3;
    } else if (*src == '+') {
      *dst++ = ' ';
      src++;
    } else {
      *dst++ = *src++;
    }
  }
  *dst = '\0';
}

// ==========================================
// 3. HTTP HANDLERS
// ==========================================

// Handler to update LCD Text
// Usage: http://IP/update_lcd?message=Line1|Line2
static esp_err_t update_lcd_handler(httpd_req_t *req) {
  char query[200] = {0};
  char param[200] = {0};
  char decoded[200] = {0};

  if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK) {
    if (httpd_query_key_value(query, "message", param, sizeof(param)) == ESP_OK) {
      
      urlDecode(decoded, param);
      
      String fullMsg = String(decoded);
      String line1 = fullMsg;
      String line2 = "";
      
      int splitIndex = fullMsg.indexOf('|');
      if (splitIndex != -1) {
        line1 = fullMsg.substring(0, splitIndex);
        line2 = fullMsg.substring(splitIndex + 1);
      }

      lcd.clear();
      lcd.setCursor(0, 0);
      lcd.print(line1.substring(0, 16)); 
      
      if (line2.length() > 0) {
        lcd.setCursor(0, 1);
        lcd.print(line2.substring(0, 16));
      }
    }
  }
  httpd_resp_send(req, "OK", 2);
  return ESP_OK;
}

// MJPEG Stream Handler
#define PART_BOUNDARY "123456789000000000000987654321"
static const char* _STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char* _STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char* _STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static esp_err_t stream_handler(httpd_req_t *req) {
  camera_fb_t * fb = NULL;
  esp_err_t res = ESP_OK;
  size_t _jpg_buf_len = 0;
  uint8_t * _jpg_buf = NULL;
  char * part_buf[64];

  res = httpd_resp_set_type(req, _STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;

  while (true) {
    fb = esp_camera_fb_get();
    if (!fb) {
      res = ESP_FAIL;
    } else {
      _jpg_buf_len = fb->len;
      _jpg_buf = fb->buf;
    }
    if (res == ESP_OK) {
      size_t hlen = snprintf((char *)part_buf, 64, _STREAM_PART, _jpg_buf_len);
      res = httpd_resp_send_chunk(req, (const char *)part_buf, hlen);
    }
    if (res == ESP_OK) {
      res = httpd_resp_send_chunk(req, (const char *)_jpg_buf, _jpg_buf_len);
      res = httpd_resp_send_chunk(req, _STREAM_BOUNDARY, strlen(_STREAM_BOUNDARY));
    }
    if (fb) {
      esp_camera_fb_return(fb);
      fb = NULL;
      _jpg_buf = NULL;
    }
    if (res != ESP_OK) break;
  }
  return res;
}

// ==========================================
// 4. SERVER INIT
// ==========================================

void startCameraServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  
  // --- SERVER 1: Control (LCD) on Port 80 ---
  config.server_port = 80;
  
  httpd_uri_t lcd_uri = {
    .uri = "/update_lcd",
    .method = HTTP_GET,
    .handler = update_lcd_handler,
    .user_ctx = NULL
  };
  
  if (httpd_start(&camera_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(camera_httpd, &lcd_uri);
  }

  // --- SERVER 2: Stream on Port 81 ---
  config.server_port = 81;
  config.ctrl_port += 1; // <--- FIX: Use a different control port (32769) to avoid conflict
  
  httpd_uri_t stream_uri = {
    .uri = "/stream",
    .method = HTTP_GET,
    .handler = stream_handler,
    .user_ctx = NULL
  };
  
  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
  }
}

// ==========================================
// 5. SETUP & LOOP
// ==========================================

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(true);
  Serial.println();

  // 1. Initialize Camera FIRST
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  
  // Low quality/size for higher frame rate
  if(psramFound()){
    config.frame_size = FRAMESIZE_VGA;
    config.jpeg_quality = 12;
    config.fb_count = 2;
  } else {
    config.frame_size = FRAMESIZE_QVGA;
    config.jpeg_quality = 12;
    config.fb_count = 1;
  }
  
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed with error 0x%x", err);
    return;
  }

  // 2. Initialize LCD (Using Custom Pins)
  Wire.begin(I2C_SDA, I2C_SCL);
  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print("Connecting WiFi");

  // 3. WiFi Connection
  WiFiManager wm;
  // wm.resetSettings(); // Uncomment to wipe credentials
  
  // Tries to connect to saved WiFi. If fails, creates AP named "ESP32-Attendance"
  bool res = wm.autoConnect("ESP32-Attendance"); 

  if(!res) {
    lcd.clear();
    lcd.print("WiFi Failed");
    ESP.restart();
  } 
  
  // 4. Start Servers
  startCameraServer();

  // 5. Display Status
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("IP Address:");
  lcd.setCursor(0, 1);
  lcd.print(WiFi.localIP());
  
  Serial.print("Stream Ready! Go to: http://");
  Serial.print(WiFi.localIP());
  Serial.println(":81/stream");
}

void loop() {
  delay(10000);
}