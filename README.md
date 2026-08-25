# Raspberry Pi Telegram Camera Bot

An aiogram 3 bot that captures photos and controls two plant pumps.
`/water_lemon` waters the lemon, `/water_pepper` waters the pepper, and each
manual watering command records and returns a video.

## Requirements

- Raspberry Pi OS Bookworm (or newer) with a supported camera
- Python 3.9+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

Test the camera before installing the bot:

```bash
rpicam-hello
```

Install the OS camera package and virtual-environment support:

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-gpiozero python3-lgpio python3-pil python3-venv ffmpeg
```

## Configure and install

From this project directory:

```bash
cp .env.example .env
nano .env
chmod +x scripts/*.sh
./scripts/install_service.sh
```

Set `BOT_TOKEN` in `.env`. For security, also set `ALLOWED_USER_IDS` to your
numeric Telegram user ID (multiple IDs may be separated with commas). You can
obtain your ID from a Telegram ID bot. If `ALLOWED_USER_IDS` is blank, anyone
who finds the bot can use the camera.

The installer creates `.venv`, installs aiogram, writes the systemd unit, and
starts it at boot. It uses `--system-site-packages` because Raspberry Pi OS
provides Picamera2 as an apt package.

## Operate

```bash
sudo systemctl status telegram-camera-bot
sudo journalctl -u telegram-camera-bot -f
sudo systemctl restart telegram-camera-bot
```

Then open the bot in Telegram. The bot registers `/start`, `/help`,
`/take_photo`, `/water_lemon`, `/water_pepper`, `/weekly_report`, and
`/schedule` in Telegram's Menu button on every startup. `/schedule` immediately
sends the latest 16 photos (or every available photo when fewer exist) and the
current sensor values to AI, installs the validated watering plan, and replies
to the requesting chat with the full schedule and human recommendations. It
always calls the API, even when the offline `AI_RESPONSE_FILE` test setting is
configured.

## Weekly automation

The service uses `Europe/Moscow` by default and runs these jobs:

- takes three daily photos at `10:30`, `12:30`, and `15:00`;
- saves every scheduled and manual photo directly in `data/photos/`;
- adds date, time, air temperature, lemon soil moisture, and pepper soil
  moisture as a watermark;
- every Monday at 10:00 selects the latest 16 JPEG files, or all available files
  when fewer than 16 exist;
- passes the weekly JSON report and all JPEG paths to `request_weekly_plan()` in
  `ai_client.py`;
- validates and persists the returned watering plan;
- runs accepted watering events on the matching plant pump;
- records every scheduled watering with the same camera workflow as the manual
  `/water_lemon` and `/water_pepper` commands;
- sends scheduled-watering MP4 videos to `REPORT_CHAT_IDS` (falling back to
  `ALLOWED_USER_IDS`);
- sends human recommendations and automation errors to `REPORT_CHAT_IDS`.

The times can be changed in `.env`:

```bash
BOT_TIMEZONE=Europe/Moscow
DAILY_PHOTO_TIMES=10:30,12:30,15:00
REPORT_CHAT_IDS=123456789
MAX_WATERING_SECONDS=30
MAX_WATERING_EVENTS=21
AI_PHOTO_LIMIT=16
TELEGRAM_STREAM_STEP_CHARS=400
TELEGRAM_STREAM_INTERVAL_SECONDS=0.3
```

Daily images, request JSON, and the accepted watering plan are stored under
`data/`. All photos are kept flat in `data/photos/`, without daily subfolders.
This directory is excluded from Git. A restart does not lose future watering
jobs: the accepted plan is restored from
`data/watering_plan.json`.

On the first start after upgrading, JPEG files from the old
`data/photos/YYYY-MM-DD/` layout are moved into the single `data/photos/`
directory and receive date-prefixed filenames.

Use `/weekly_report` to run the weekly cycle manually. It requires at least one
archived photo.

### AI integration and JSON

`ai_client.py` uses the OpenAI Python SDK with Yandex AI Studio's compatible
Responses endpoint. One request contains the weekly report, response JSON
Schema, and up to 16 latest JPEG files as base64 image inputs.
The reusable prompt, project, endpoint, timeout, and image detail are configured
in `.env`:

```bash
YANDEX_AI_API_KEY=replace_with_your_real_key
YANDEX_AI_BASE_URL=https://ai.api.cloud.yandex.net/v1
YANDEX_AI_PROJECT_ID=b1gr38liecpk6mp2g7ul
YANDEX_AI_PROMPT_ID=fvthisds2b24do0qnn7q
YANDEX_AI_IMAGE_DETAIL=low
YANDEX_AI_IMAGE_MAX_WIDTH=1280
YANDEX_AI_IMAGE_MAX_HEIGHT=720
YANDEX_AI_IMAGE_JPEG_QUALITY=75
YANDEX_AI_MAX_OUTPUT_TOKENS=4000
YANDEX_AI_TIMEOUT_SECONDS=300
YANDEX_AI_REQUEST_RETRIES=1
```

Keep `.env` private; it is excluded from Git and the installer sets mode `600`.
The model response is parsed as JSON and then checked against the bot's pump,
time-window, duration, event-count, and plant-to-pump safety rules before any
watering jobs are created.

All selected photos are still sent, up to `AI_PHOTO_LIMIT=16`. The code applies
a hard maximum of 16 even if an older `.env` contains a larger value. Before upload,
temporary API copies are resized to fit within 1280×720 and saved as quality-75
JPEGs. The dated originals and their watermarks are not modified. Native
Responses API JSON Schema output is used instead of repeating the full schema
inside the text prompt, and the response is limited to 4000 tokens.

The request does not include `tools`: Yandex AI Studio cannot combine tools and
the JSON Schema response format used by this bot.

The bot treats a returned `status: failed` as a provider error instead of an
empty non-JSON answer. Transient errors such as `model_call_error`, HTTP 5xx,
timeouts, and rate limits are retried according to
`YANDEX_AI_REQUEST_RETRIES`; every physical attempt has a separate dated log.

Every real AI API call is archived under
`data/ai_logs/YYYY-MM-DD/<timestamp>/`. Each directory contains:

- `request.json` — dated request parameters, report, schema, and photo
  metadata (without the API key or duplicate base64 image data);
- `response.txt` — the complete unmodified text returned by AI;
- `response.json` — the complete SDK response, including metadata and usage when
  supplied by the provider;
- `status.json` — timestamps, duration, and the final request status (including
  `provider_error` when the API returns `status: failed`);
- `parsed_plan.json` for a valid JSON response, or `error.json` if the API call
  failed.

The original JPEG files referenced by each request remain in `data/photos/`.

If the model returns text instead of a JSON object, the bot sends the complete
raw response to Telegram. In a private chat it first streams the text through
Telegram message drafts and then sends normal permanent messages. If drafts are
unavailable (for example, in a group), it automatically falls back to permanent
messages. Responses longer than one Telegram message are split without dropping
content. Streaming chunk size and delay can be tuned with
`TELEGRAM_STREAM_STEP_CHARS` and `TELEGRAM_STREAM_INTERVAL_SECONDS`.

The response schema deliberately contains no conditional `if`/`then` rules.
Plant-to-pump matching (`lemon` → `pump_lemon`, `pepper` → `pump_pepper`) is
stated in the AI instruction and enforced independently by `automation.py`
after JSON parsing.

For an offline test without making an API request, point `AI_RESPONSE_FILE` at
a response JSON file. This setting takes precedence over the API:

```bash
AI_RESPONSE_FILE=/absolute/path/to/schemas/weekly_plan.example.json
```

Update the example's dates to the current week before testing. Contracts are in:

- `schemas/weekly_request.example.json` — data and media metadata sent to AI;
- `schemas/weekly_plan.example.json` — example AI response;
- `schemas/weekly_plan.schema.json` — formal response schema.

Sensor values remain `null` with `sensor_status: "not_connected"` until physical
sensors are implemented. For temporary testing, use
`MOCK_AIR_TEMPERATURE_C`, `MOCK_LEMON_SOIL_MOISTURE_PERCENT`, and
`MOCK_PEPPER_SOIL_MOISTURE_PERCENT` in `.env`.

After configuration changes, reinstall Python requirements and restart:

```bash
./scripts/update_service.sh
sudo journalctl -u telegram-camera-bot -f
```

`update_service.sh` updates the virtual-environment dependencies, checks all
Python modules, reloads systemd units, enables the service at boot, restarts it,
and prints diagnostics if startup fails. Use `install_service.sh` only for the
first installation or to recreate the systemd unit.

## Pump wiring and safety

`LEMON_PUMP_GPIO=17` uses **BCM GPIO17** (physical header pin 11).
`PEPPER_PUMP_GPIO` must be set to the BCM pin actually connected to the second
relay. It is deliberately blank by default, so pepper watering remains disabled
until the real pin is configured. The two GPIO numbers must be different.

The lemon relay is controlled by toggle pulses: one GPIO17 LOW/HIGH/LOW pulse
starts the pump and a second pulse stops it after the watering interval. The
pepper output is active-high: it remains LOW while idle and is held HIGH for
the watering interval. Do not
power a pump directly from a GPIO pin. Use suitable relays or MOSFET drivers, a
separate pump power supply, flyback protection for inductive DC loads, and a
common ground where required by the drivers. Verify safe startup behavior before
leaving the system unattended.

Configure both pumps and manual durations in `.env`, then restart the service:

```bash
LEMON_PUMP_GPIO=17
PEPPER_PUMP_GPIO=27
LEMON_WATERING_SECONDS=5
PEPPER_WATERING_SECONDS=5
LEMON_TOGGLE_PULSE_SECONDS=0.2
```

Replace `27` with the actual BCM pin used by the pepper pump.

`LEMON_TOGGLE_PULSE_SECONDS` controls how long the GPIO17 logical ON part of
each toggle pulse lasts. The default is 200 ms.

```bash
sudo systemctl restart telegram-camera-bot
```

Watering video defaults to 640×480 at 24 fps and 2 Mbps for quick camera startup
and upload. It includes a one-second lead-in and half-second lead-out so the full
five-second pump cycle is visible. These values can be adjusted with the
`VIDEO_*` settings shown in `.env.example`.

To remove only the systemd service (preserving the project and configuration):

```bash
./scripts/uninstall_service.sh
```
