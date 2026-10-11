# Raspberry Pi Telegram Camera Bot

An aiogram 3 bot that captures photos and controls two plant pumps.
`/water_lemon` waters the lemon, `/water_pepper` waters the pepper, and each
manual watering command records and returns a video.

## Requirements

- Raspberry Pi OS Bookworm (or newer) with a supported camera
- Python 3.10+
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
`/take_photo`, `/water_lemon`, `/water_pepper`, `/get_schedule`, and
`/read_schedule` in Telegram's Menu button on every startup. `/get_schedule` immediately
sends up to seven photos at `PICK_DAILY_PHOTO_TIME` from the past seven days (one per day) and the
current sensor values to AI, installs the validated watering plan, and replies
to the requesting chat with the full schedule and human recommendations. It
always calls the API, even when the offline `AI_RESPONSE_FILE` test setting is
configured.

`/get_schedule` replaces the current watering plan after validation, then sends
the plan and recommendations only to the chat that invoked it. If the request or
installation fails, the previous plan stays in place. `/read_schedule` reads
`data/watering_plan.json` and sends the last accepted plan to the calling chat;
it does not call AI, take photos, save files or change watering jobs. It also
shows plans from previous weeks, with elapsed events marked as past. The former
`/weekly_report` and `/schedule` commands have been removed.

## Weekly automation

The service uses `Europe/Moscow` by default and runs these jobs:

- takes photos every hour from `08:00` through `20:00` inclusive (13 per day);
- saves every scheduled and manual photo directly in `data/photos/`;
- adds date, time, air temperature, lemon soil moisture, and pepper soil
  moisture as a watermark;
- every Monday at 10:00 selects one photo taken at `PICK_DAILY_PHOTO_TIME`
  (default `15:00`) per day from the
  preceding seven days, up to seven photos;
- passes the weekly JSON report and all JPEG paths to `request_weekly_plan()` in
  `ai_client.py`;
- validates and persists the returned watering plan;
- runs accepted watering events on the matching plant pump;
- records every scheduled watering with the same camera workflow as the manual
  `/water_lemon` and `/water_pepper` commands;
- sends scheduled-watering MP4 videos to `REPORT_CHAT_IDS` (falling back to
  `ALLOWED_USER_IDS`);
- sends the full watering schedule followed by human recommendations to
  `REPORT_CHAT_IDS`, along with automation errors when they occur.

Automatic weekly reports, `/get_schedule`, and `/read_schedule` use the same readable
Telegram format: a week-start heading, one line per watering event with its date,
time, plant, duration and status, then a `Рекомендации:` section. JSON remains the
internal format used to validate, save and schedule watering. Long reports are
split across messages without truncation. Automatic Monday reports still go to
`REPORT_CHAT_IDS`; manual schedule commands reply only to the calling chat.

The times can be changed in `.env`:

```bash
BOT_TIMEZONE=Europe/Moscow
DAILY_PHOTO_TIMES=08:00,09:00,10:00,11:00,12:00,13:00,14:00,15:00,16:00,17:00,18:00,19:00,20:00
PICK_DAILY_PHOTO_TIME=15:00
REPORT_CHAT_IDS=123456789
MAX_WATERING_SECONDS=30
MAX_WATERING_EVENTS=21
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

Use `/get_schedule` to run the weekly cycle manually. It requires at least one
photo at `PICK_DAILY_PHOTO_TIME` from the preceding seven days.
Capture times come from archive filenames in `BOT_TIMEZONE`, not file modification
times. Only photos from the configured minute qualify; if there are duplicates, the
earliest one is used. Missing days are not filled with older photos or photos at other times.
The selection window is `(report time - 7 days, report time]`, so a Monday 10:00
report uses 15:00 photos from the previous Monday through Sunday with the default
configuration. Manual reports after the configured time can include today's photo.
Set `PICK_DAILY_PHOTO_TIME` in `.env` using `HH:MM` and include that time in
`DAILY_PHOTO_TIMES` so the required photos are captured. Restart the service after
changing either setting.

When upgrading an existing Raspberry Pi installation, update `DAILY_PHOTO_TIMES`
in the project's `.env` to the hourly list above and run
`./scripts/update_service.sh` with the updated project code. The old restriction
requiring exactly three times has been removed. An old `AI_PHOTO_LIMIT` setting
is no longer used and can be deleted.

### AI integration and JSON

`ai_client.py` uses the OpenAI Python SDK with Yandex AI Studio's compatible
Responses endpoint. One request contains the weekly report, response JSON
Schema, and up to seven JPEG files taken at the configured time as base64 image inputs.
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

All selected photos are sent, up to seven from the preceding seven days. Before upload,
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
- `response.txt` — the complete text returned by AI;
- `response.json` — the complete SDK response, including metadata and usage when
  supplied by the provider;
- `status.json` — timestamps, duration, and the final request status (including
  `provider_error` when the API returns `status: failed`);
- `parsed_plan.json` for a valid JSON response, or `error.json` if the API call
  failed.

The configured API key is replaced with `[REDACTED]` if a provider echoes it in
the archived response or error details. Other response fields are retained.
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
a response JSON file. This setting takes precedence over the API for the
automatic weekly cycle; `/get_schedule` always requests a new plan through the API:

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
relay. The example uses `27`; leave it blank to disable pepper watering until the
real pin is configured. The two GPIO numbers must be different.

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
configured pump cycle is visible. These values can be adjusted with the
`VIDEO_*` settings shown in `.env.example`.

To remove only the systemd service (preserving the project and configuration):

```bash
./scripts/uninstall_service.sh
```

## Verification and maintenance

Run regression tests with the Python dependencies installed (no connected camera,
GPIO, Telegram token or AI credentials are needed):

```bash
python -m unittest discover -s tests -v
```

The tests use real aiogram, APScheduler and Pillow with mocked network and
hardware boundaries. They cover command routing, read-only schedule access,
plan replacement rollback, photo selection, AI responses/logs, and cleanup on
cancellation. Hardware workers finish their cleanup before releasing camera/pump
locks or temporary video files. Shutdown waits for those workers before closing
GPIO resources, following Python's [task cancellation and shielding guidance](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation).

Plan replacement does not interrupt a watering already in progress; an old job
still waiting for the camera or pump is discarded after a newer plan is accepted.
Photos appear in the archive only after capture and watermarking have completed.
An abrupt power loss cannot be handled by software cleanup: a toggle relay's
physical state cannot be inferred from the GPIO output alone.
