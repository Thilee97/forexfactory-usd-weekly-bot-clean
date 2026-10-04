from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import cloudscraper
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont


FF_THISWEEK_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_CALENDAR_URL = "https://www.forexfactory.com/calendar?week={week}"
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

STATE_PATH = Path("state/state.json")
IMAGE_PATH = Path("usd_calendar_week.png")

IMAGE_CURRENCY = "USD"
NOTIFY_IMPACTS = {"High", "Medium"}
REMINDER_MINUTES = (30, 10)
ACTUAL_LOOKBACK_MINUTES = 45
ACTUAL_MAX_ATTEMPTS = 4
WEEKLY_SEND_WEEKDAY = 0  # 0 = Thứ Hai (Monday)
WEEKLY_SEND_HOUR = 6     # 06:00 AM đầu tuần
DAILY_SEND_HOUR = 6      # 06:00 AM mỗi ngày


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
CHAT_ID = require_env("TELEGRAM_CHAT_ID")
FORCE_WEEKLY = os.getenv("FORCE_WEEKLY", "false").lower() == "true"
FORCE_DAILY = os.getenv("FORCE_DAILY", "false").lower() == "true"


def telegram_url(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def send_message(text: str, remove_keyboard: bool = True) -> None:
    data = {"chat_id": CHAT_ID, "text": text}
    if remove_keyboard:
        data["reply_markup"] = json.dumps({"remove_keyboard": True})
    r = requests.post(
        telegram_url("sendMessage"),
        data=data,
        timeout=30,
    )
    r.raise_for_status()


def send_photo(path: Path, caption: str, remove_keyboard: bool = True) -> None:
    data = {"chat_id": CHAT_ID, "caption": caption}
    if remove_keyboard:
        data["reply_markup"] = json.dumps({"remove_keyboard": True})
    with path.open("rb") as f:
        r = requests.post(
            telegram_url("sendPhoto"),
            data=data,
            files={"photo": (path.name, f, "image/png")},
            timeout=60,
        )
    r.raise_for_status()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "weekly_sent": "",
            "daily_sent": "",
            "reminders": {},
            "actual_sent": {},
            "actual_attempts": {},
        }
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        s.setdefault("weekly_sent", "")
        s.setdefault("daily_sent", "")
        s.setdefault("reminders", {})
        s.setdefault("actual_sent", {})
        s.setdefault("actual_attempts", {})
        return s
    except Exception:
        return {
            "weekly_sent": "",
            "daily_sent": "",
            "reminders": {},
            "actual_sent": {},
            "actual_attempts": {},
        }


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def event_id(event: dict[str, Any]) -> str:
    raw = f"{event.get('country','')}|{event.get('title','')}|{event.get('date','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]



def fetch_weekly_events() -> list[dict[str, Any]]:
    """Fetch the official Forex Factory export for the current week."""
    r = requests.get(
        FF_THISWEEK_JSON_URL,
        headers={"User-Agent": "Mozilla/5.0 forexfactory-usd-weekly-bot/1.0"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("Forex Factory weekly JSON did not return a list.")

    events: list[dict[str, Any]] = []
    for raw in data:
        if raw.get("country") != IMAGE_CURRENCY:
            continue
        try:
            source_dt = datetime.fromisoformat(raw["date"])
        except Exception:
            continue
        event = dict(raw)
        event["source_dt"] = source_dt
        event["local_dt"] = source_dt.astimezone(LOCAL_TZ)
        event["id"] = event_id(event)
        event["actual"] = ""
        events.append(event)

    events.sort(key=lambda e: e["source_dt"])
    return events


def _impact_from_cell(cell) -> str:
    if cell is None:
        return ""
    parts = [cell.get_text(" ", strip=True), cell.get("title", "")]
    for node in cell.find_all(True):
        parts.append(node.get("title", ""))
        parts.extend(node.get("class", []))
    probe = " ".join(str(x) for x in parts).lower()
    if "high" in probe or "impact-red" in probe:
        return "High"
    if "medium" in probe or "med impact" in probe or "impact-ora" in probe or "impact-orange" in probe:
        return "Medium"
    if "low" in probe or "impact-yel" in probe or "impact-yellow" in probe:
        return "Low"
    return ""


def _page_timezone(soup) -> ZoneInfo:
    page_text = soup.get_text(" ", strip=True)
    match = re.search(r"Calendar Time Zone:\s*([A-Za-z_]+/[A-Za-z_]+)", page_text)
    if match:
        try:
            return ZoneInfo(match.group(1))
        except Exception:
            pass
    return ZoneInfo("America/New_York")


def _next_sunday(base_date):
    days = (6 - base_date.weekday()) % 7
    if days == 0:
        days = 7
    return base_date + timedelta(days=days)


def _parse_row_datetime(row, date_text: str, time_text: str, page_tz: ZoneInfo, target_sunday):
    candidates = [row.get("data-event-datetime", "")]
    for node in row.find_all(True):
        value = node.get("data-event-datetime", "")
        if value:
            candidates.append(value)
    for raw in candidates:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=page_tz)
            local = dt.astimezone(LOCAL_TZ)
            return local, local.strftime("%a %d/%m  %H:%M")
        except Exception:
            pass

    date_match = re.search(r"(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat)?\s*([A-Za-z]{3})\s+(\d{1,2})", date_text or "")
    if date_match:
        mon, day = date_match.groups()
        dates = []
        for year in (target_sunday.year - 1, target_sunday.year, target_sunday.year + 1):
            try:
                dates.append(datetime.strptime(f"{mon} {day} {year}", "%b %d %Y").date())
            except Exception:
                pass
        event_date = min(dates, key=lambda d: abs((d - target_sunday).days)) if dates else target_sunday
    else:
        event_date = target_sunday

    raw_time = (time_text or "").strip()
    normalized = raw_time.replace(" ", "").upper()
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            tm = datetime.strptime(normalized, fmt).time()
            dt = datetime.combine(event_date, tm, tzinfo=page_tz).astimezone(LOCAL_TZ)
            return dt, dt.strftime("%a %d/%m  %H:%M")
        except Exception:
            pass

    dt = datetime.combine(event_date, datetime.min.time(), tzinfo=page_tz).astimezone(LOCAL_TZ)
    label = f"{dt.strftime('%a %d/%m')}  {raw_time or 'Tentative'}"
    return dt, label


def fetch_next_week_events() -> list[dict[str, Any]]:
    """Fetch next week's USD events from the Forex Factory calendar page.

    Forex Factory's export link is `ff_calendar_thisweek.json`; there is no
    `ff_calendar_nextweek.json`, so next week is read from `calendar?week=next`
    only when the user presses the Telegram button.
    """
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )
    r = scraper.get(FF_CALENDAR_URL.format(week="next"), timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", {"class": "calendar__table"})
    if table is None:
        raise RuntimeError("Could not locate Forex Factory next-week calendar table.")

    page_tz = _page_timezone(soup)
    target_sunday = _next_sunday(datetime.now(page_tz).date())
    current_date = ""
    current_time = ""
    events: list[dict[str, Any]] = []

    for row in table.find_all("tr", {"class": "calendar__row"}):
        def cell_text(cls: str) -> str:
            cell = row.find("td", {"class": cls})
            return cell.get_text(" ", strip=True) if cell else ""

        date_text = cell_text("calendar__date")
        time_text = cell_text("calendar__time")
        if date_text:
            current_date = date_text
        if time_text:
            current_time = time_text

        currency = cell_text("calendar__currency")
        title = cell_text("calendar__event")
        if currency != IMAGE_CURRENCY or not title:
            continue

        local_dt, display_time = _parse_row_datetime(
            row, current_date, current_time, page_tz, target_sunday
        )
        impact_cell = row.find("td", {"class": "calendar__impact"})
        event = {
            "title": title,
            "country": currency,
            "date": local_dt.isoformat(),
            "impact": _impact_from_cell(impact_cell),
            "actual": cell_text("calendar__actual"),
            "forecast": cell_text("calendar__forecast"),
            "previous": cell_text("calendar__previous"),
            "source_dt": local_dt,
            "local_dt": local_dt,
            "display_time": display_time,
        }
        event["id"] = event_id(event)
        events.append(event)

    events.sort(key=lambda e: e["local_dt"])
    if not events:
        raise RuntimeError("Next-week page was read, but no USD events were found.")
    return events

def week_key(events: list[dict[str, Any]]) -> str:
    if not events:
        now = datetime.now(LOCAL_TZ)
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    start = min(e["local_dt"].date() for e in events)
    end = max(e["local_dt"].date() for e in events)
    return f"{start.isoformat()}_{end.isoformat()}"


def ff_week_parameter(events: list[dict[str, Any]]) -> str:
    if not events:
        return datetime.now().strftime("%b%d.%Y").lower()
    first_source_date = min(e["source_dt"] for e in events).date()
    return first_source_date.strftime("%b%d.%Y").lower()


def scrape_current_week() -> list[dict[str, str]]:
    events = fetch_weekly_events()
    week = ff_week_parameter(events)
    url = FF_CALENDAR_URL.format(week=week)

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )
    r = scraper.get(url, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", {"class": "calendar__table"})
    if table is None:
        raise RuntimeError("Could not locate Forex Factory calendar table.")

    rows = table.find_all("tr", {"class": "calendar__row"})
    result: list[dict[str, str]] = []

    for row in rows:
        def cell_text(cls: str) -> str:
            cell = row.find("td", {"class": cls})
            return cell.get_text(" ", strip=True) if cell else ""

        currency = cell_text("calendar__currency")
        title = cell_text("calendar__event")
        if not currency or not title:
            continue

        result.append(
            {
                "currency": currency,
                "title": title,
                "actual": cell_text("calendar__actual"),
                "forecast": cell_text("calendar__forecast"),
                "previous": cell_text("calendar__previous"),
            }
        )
    return result


def merge_live_values(events: list[dict[str, Any]], live_rows: list[dict[str, str]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in live_rows:
        groups[(row["currency"], row["title"])].append(row)

    used: dict[tuple[str, str], int] = defaultdict(int)
    for event in events:
        key = (event["country"], event["title"])
        idx = used[key]
        rows = groups.get(key, [])
        if idx < len(rows):
            row = rows[idx]
            event["actual"] = row.get("actual", "")
            if row.get("forecast"):
                event["forecast"] = row["forecast"]
            if row.get("previous"):
                event["previous"] = row["previous"]
            used[key] += 1


def impact_symbol(impact: str) -> str:
    return {"High": "🔴", "Medium": "🟠", "Low": "🟡"}.get(impact, "⚪")


def impact_label(impact: str) -> str:
    return {"High": "HIGH", "Medium": "MED", "Low": "LOW"}.get(impact, impact.upper())


def value_or_dash(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "—"


def parse_numeric(value: str) -> float | None:
    value = (value or "").strip().replace(",", "")
    if not value:
        return None
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*([KMBT%]?)", value, re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).upper()
    mult = {"": 1, "%": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[unit]
    return number * mult


def comparison_text(actual: str, forecast: str) -> str:
    a = parse_numeric(actual)
    f = parse_numeric(forecast)
    if a is None or f is None:
        return ""
    if a > f:
        return "📈 Actual cao hơn Forecast"
    if a < f:
        return "📉 Actual thấp hơn Forecast"
    return "➖ Actual bằng Forecast"


def font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def truncate(draw: ImageDraw.ImageDraw, text: str, max_width: int, fnt) -> str:
    if draw.textbbox((0, 0), text, font=fnt)[2] <= max_width:
        return text
    suffix = "…"
    while text:
        text = text[:-1]
        if draw.textbbox((0, 0), text + suffix, font=fnt)[2] <= max_width:
            return text + suffix
    return suffix


def render_week_image(events: list[dict[str, Any]], output: Path) -> None:
    width = 1500
    header_h = 125
    columns_h = 58
    row_h = 62
    footer_h = 50
    height = header_h + columns_h + row_h * max(1, len(events)) + footer_h

    img = Image.new("RGB", (width, height), (245, 247, 250))
    draw = ImageDraw.Draw(img)

    f_title = font(38, bold=True)
    f_sub = font(22)
    f_head = font(22, bold=True)
    f_row = font(22)
    f_small = font(18)

    if events:
        start = min(e["local_dt"].date() for e in events)
        end = max(e["local_dt"].date() for e in events)
        date_range = f"{start.strftime('%d/%m/%Y')} - {end.strftime('%d/%m/%Y')}"
    else:
        date_range = "Không có dữ liệu"

    draw.rectangle((0, 0, width, header_h), fill=(41, 61, 91))
    draw.text((35, 25), "FOREX FACTORY — USD WEEKLY CALENDAR", font=f_title, fill="white")
    draw.text(
        (37, 78),
        f"Tuần {date_range}  •  Giờ Việt Nam (GMT+7)",
        font=f_sub,
        fill=(225, 232, 242),
    )

    cols = [
        ("Thời gian", 35),
        ("Impact", 220),
        ("Sự kiện", 360),
        ("Actual", 1020),
        ("Forecast", 1160),
        ("Previous", 1310),
    ]

    y = header_h
    draw.rectangle((0, y, width, y + columns_h), fill=(220, 226, 235))
    for name, x in cols:
        draw.text((x, y + 15), name, font=f_head, fill=(35, 45, 60))

    y += columns_h
    for i, event in enumerate(events):
        bg = (255, 255, 255) if i % 2 == 0 else (238, 242, 247)
        draw.rectangle((0, y, width, y + row_h), fill=bg)

        time_label = event.get("display_time") or event["local_dt"].strftime("%a %d/%m  %H:%M")
        draw.text(
            (35, y + 17),
            time_label,
            font=f_row,
            fill=(25, 35, 48),
        )

        impact = event.get("impact", "")
        impact_fill = {
            "High": (196, 48, 43),
            "Medium": (231, 132, 31),
            "Low": (222, 180, 38),
        }.get(impact, (130, 140, 150))

        cx, cy, radius = 278, y + 31, 10
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=impact_fill)

        title = truncate(draw, event.get("title", ""), 625, f_row)
        draw.text((360, y + 17), title, font=f_row, fill=(25, 35, 48))
        draw.text((1020, y + 17), value_or_dash(event.get("actual")), font=f_row, fill=(25, 35, 48))
        draw.text((1160, y + 17), value_or_dash(event.get("forecast")), font=f_row, fill=(25, 35, 48))
        draw.text((1310, y + 17), value_or_dash(event.get("previous")), font=f_row, fill=(25, 35, 48))

        y += row_h

    draw.text(
        (35, height - 36),
        "Nguồn: Forex Factory / Fair Economy  •  Thời gian có thể thay đổi",
        font=f_small,
        fill=(90, 100, 112),
    )
    img.save(output, "PNG", optimize=True)


def send_weekly_calendar(
    events: list[dict[str, Any]],
    state: dict[str, Any],
    key: str,
    title: str = "📅 LỊCH KINH TẾ USD TRONG TUẦN",
    mark_sent: bool = True,
) -> None:
    if not events:
        send_message(f"{title}\n\n🟢 Tuần này không có sự kiện kinh tế USD nào trên lịch.")
        if mark_sent:
            state["weekly_sent"] = key
        return

    start_date = min(e["local_dt"].date() for e in events)
    end_date = max(e["local_dt"].date() for e in events)

    events_by_date: dict[Any, list[dict[str, Any]]] = {}
    for e in events:
        d = e["local_dt"].date()
        events_by_date.setdefault(d, []).append(e)

    high_count = sum(1 for e in events if e.get("impact") == "High")
    med_count = sum(1 for e in events if e.get("impact") == "Medium")
    low_count = sum(1 for e in events if e.get("impact") == "Low")

    header = (
        f"{title}\n"
        f"🗓 {start_date.strftime('%d/%m')} – {end_date.strftime('%d/%m/%Y')}\n"
        f"📊 Tổng: {len(events)} sự kiện (🔴 {high_count} High | 🟠 {med_count} Medium | 🟡 {low_count} Low)\n"
        f"🕒 Giờ Việt Nam (GMT+7)\n"
        f"════════════════════════"
    )

    day_blocks = []
    weekday_vn = [
        "Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm",
        "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"
    ]

    for d in sorted(events_by_date.keys()):
        day_name = weekday_vn[d.weekday()]
        d_str = d.strftime("%d/%m")
        day_events = sorted(events_by_date[d], key=lambda e: e["local_dt"])

        ev_lines = []
        for e in day_events:
            t_str = e["local_dt"].strftime("%H:%M")
            impact = e.get("impact", "")
            icon = "🔴" if impact == "High" else ("🟠" if impact == "Medium" else "🟡")
            title_ev = e.get("title", "")
            fc = value_or_dash(e.get("forecast"))
            prev = value_or_dash(e.get("previous"))
            ev_lines.append(
                f"  • {t_str} | {icon} {title_ev}\n    ↳ Dự báo: {fc} | Trước đó: {prev}"
            )

        block = f"🗓 {day_name.upper()} ({d_str}):\n\n" + "\n\n".join(ev_lines)
        day_blocks.append(block)

    full_content = (
        header
        + "\n\n"
        + "\n\n────────────────────────\n\n".join(day_blocks)
        + "\n\n════════════════════════\n🕒 Tự động gửi lúc 06:00 Thứ Hai • Nguồn: Forex Factory"
    )

    if len(full_content) <= 4000:
        send_message(full_content)
    else:
        chunks = []
        cur = header
        for b in day_blocks:
            piece = "\n\n────────────────────────\n\n" + b
            if len(cur) + len(piece) > 3800:
                chunks.append(cur)
                cur = b
            else:
                cur += piece
        if cur:
            cur += "\n\n════════════════════════\nNguồn: Forex Factory"
            chunks.append(cur)
        for c in chunks:
            send_message(c)

    if mark_sent:
        state["weekly_sent"] = key
    print(f"Sent weekly calendar text for week {key}")


def send_weekly_image(
    events: list[dict[str, Any]],
    state: dict[str, Any],
    key: str,
    title: str = "📅 LỊCH KINH TẾ USD TRONG TUẦN",
    mark_sent: bool = True,
    include_live: bool = True,
) -> None:
    send_weekly_calendar(events, state, key, title=title, mark_sent=mark_sent)


def send_next_week_image(state: dict[str, Any]) -> None:
    events = fetch_next_week_events()
    key = f"next_{week_key(events)}"
    send_weekly_image(
        events,
        state,
        key,
        title="⏭ LỊCH KINH TẾ USD TUẦN SAU",
        mark_sent=False,
        include_live=False,
    )

def process_reminders(events: list[dict[str, Any]], state: dict[str, Any], now: datetime) -> None:
    reminder_state = state.setdefault("reminders", {})

    for event in events:
        if event.get("impact") not in NOTIFY_IMPACTS:
            continue

        minutes_to = (event["local_dt"] - now).total_seconds() / 60
        eid = event["id"]
        sent = set(reminder_state.get(eid, []))

        for threshold in REMINDER_MINUTES:
            if threshold - 5 < minutes_to <= threshold and threshold not in sent:
                msg = (
                    f"⏰ TIN USD SẮP RA — còn khoảng {threshold} phút\n\n"
                    f"{impact_symbol(event.get('impact',''))} {event.get('impact','')} Impact\n"
                    f"🇺🇸 {event.get('title','')}\n"
                    f"🕒 {event['local_dt'].strftime('%H:%M - %d/%m/%Y')} (GMT+7)\n\n"
                    f"Forecast: {value_or_dash(event.get('forecast'))}\n"
                    f"Previous: {value_or_dash(event.get('previous'))}\n"
                    "Nguồn: Forex Factory"
                )
                send_message(msg)
                sent.add(threshold)
                reminder_state[eid] = sorted(sent)


def process_actuals(events: list[dict[str, Any]], state: dict[str, Any], now: datetime) -> None:
    """Low-request Actual polling.

    The weekly JSON handles normal schedule/reminder work.
    Live Forex Factory HTML is fetched only when a USD High/Medium event:
    - has reached release time,
    - is within 45 minutes after release,
    - has not sent Actual yet,
    - has fewer than 4 prior attempts.

    One page fetch serves all eligible events in that run.
    """
    attempts = state.setdefault("actual_attempts", {})
    actual_sent = state.setdefault("actual_sent", {})

    candidates: list[dict[str, Any]] = []
    for event in events:
        if event.get("impact") not in NOTIFY_IMPACTS:
            continue
        if actual_sent.get(event["id"]):
            continue

        age_minutes = (now - event["local_dt"]).total_seconds() / 60
        if age_minutes < 0 or age_minutes > ACTUAL_LOOKBACK_MINUTES:
            continue

        current_attempts = int(attempts.get(event["id"], 0) or 0)
        if current_attempts >= ACTUAL_MAX_ATTEMPTS:
            continue

        candidates.append(event)

    if not candidates:
        return

    try:
        live_rows = scrape_current_week()
    except Exception as exc:
        print(f"[warning] Could not scrape live Actual values: {exc}", file=sys.stderr)
        for event in candidates:
            attempts[event["id"]] = int(attempts.get(event["id"], 0) or 0) + 1
        return

    merge_live_values(events, live_rows)
    by_id = {e["id"]: e for e in events}

    for candidate in candidates:
        eid = candidate["id"]
        event = by_id[eid]
        attempts[eid] = int(attempts.get(eid, 0) or 0) + 1

        actual = str(event.get("actual") or "").strip()
        if not actual:
            continue

        comparison = comparison_text(actual, str(event.get("forecast") or ""))
        msg = (
            "🚨 KẾT QUẢ TIN USD\n\n"
            f"{impact_symbol(event.get('impact',''))} {event.get('impact','')} Impact\n"
            f"🇺🇸 {event.get('title','')}\n"
            f"🕒 {event['local_dt'].strftime('%H:%M - %d/%m/%Y')} (GMT+7)\n\n"
            f"Actual: {value_or_dash(actual)}\n"
            f"Forecast: {value_or_dash(event.get('forecast'))}\n"
            f"Previous: {value_or_dash(event.get('previous'))}"
        )
        if comparison:
            msg += f"\n\n{comparison}"
        msg += "\n\nNguồn: Forex Factory"

        send_message(msg)
        actual_sent[eid] = actual

def send_daily_calendar(events: list[dict[str, Any]], state: dict[str, Any], now: datetime) -> None:
    today = now.date()
    today_str = today.isoformat()
    today_events = [e for e in events if e["local_dt"].date() == today]

    weekday_vn = [
        "Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm",
        "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"
    ][today.weekday()]
    date_formatted = today.strftime("%d/%m/%Y")

    if not today_events:
        msg = (
            f"📅 LỊCH KINH TẾ USD HÔM NAY ({weekday_vn}, {date_formatted})\n\n"
            "🟢 Hôm nay không có sự kiện kinh tế USD nào trên lịch.\n\n"
            "🕒 Tự động gửi lúc 06:00 (GMT+7) • Nguồn: Forex Factory"
        )
    else:
        today_events.sort(key=lambda e: e["local_dt"])
        lines = []
        for e in today_events:
            t_str = e["local_dt"].strftime("%H:%M")
            impact = e.get("impact", "")
            icon = "🔴" if impact == "High" else ("🟠" if impact == "Medium" else "🟡")
            title = e.get("title", "")
            fc = value_or_dash(e.get("forecast"))
            prev = value_or_dash(e.get("previous"))
            lines.append(
                f"• {t_str} | {icon} {title}\n  ↳ Dự báo: {fc} | Trước đó: {prev}"
            )

        events_text = "\n\n".join(lines)
        high_count = sum(1 for e in today_events if e.get("impact") == "High")
        med_count = sum(1 for e in today_events if e.get("impact") == "Medium")
        low_count = sum(1 for e in today_events if e.get("impact") == "Low")

        msg = (
            f"📅 LỊCH KINH TẾ USD HÔM NAY ({weekday_vn}, {date_formatted})\n"
            f"📊 Tổng cộng: {len(today_events)} sự kiện (🔴 {high_count} High | 🟠 {med_count} Medium | 🟡 {low_count} Low)\n"
            "────────────────────────\n\n"
            f"{events_text}\n\n"
            "────────────────────────\n"
            "🕒 Tự động gửi lúc 06:00 (GMT+7) • Nguồn: Forex Factory"
        )

    send_message(msg)
    state["daily_sent"] = today_str
    print(f"Sent daily calendar for {today_str}")


def prune_state(state: dict[str, Any], valid_ids: set[str]) -> None:
    state["reminders"] = {
        k: v for k, v in state.get("reminders", {}).items() if k in valid_ids
    }
    state["actual_sent"] = {
        k: v for k, v in state.get("actual_sent", {}).items() if k in valid_ids
    }
    state["actual_attempts"] = {
        k: v for k, v in state.get("actual_attempts", {}).items() if k in valid_ids
    }


def main() -> None:
    now = datetime.now(LOCAL_TZ)
    state = load_state()
    events = fetch_weekly_events()
    key = week_key(events)
    valid_ids = {e["id"] for e in events}
    prune_state(state, valid_ids)

    # 1. Gửi lịch Tuần (Thứ Hai lúc 06:00 sáng)
    should_send_weekly = (
        FORCE_WEEKLY
        or (
            now.weekday() == WEEKLY_SEND_WEEKDAY
            and now.hour >= WEEKLY_SEND_HOUR
            and state.get("weekly_sent") != key
        )
    )
    if should_send_weekly:
        print(f"Sending weekly USD calendar image for week {key}...")
        send_weekly_image(events, state, key)

    # 2. Gửi lịch Ngày (Mỗi ngày lúc 06:00 sáng)
    today_str = now.date().isoformat()
    should_send_daily = (
        FORCE_DAILY
        or (
            now.hour >= DAILY_SEND_HOUR
            and state.get("daily_sent") != today_str
        )
    )
    if should_send_daily:
        print(f"Sending daily USD calendar for {today_str}...")
        send_daily_calendar(events, state, now)

    # 3. Quản lý cảnh báo trước giờ ra tin và cập nhật số liệu Actual
    process_reminders(events, state, now)
    process_actuals(events, state, now)
    save_state(state)

    print(
        f"Done. USD events={len(events)}, week={key}, "
        f"time={now.isoformat(timespec='seconds')}"
    )


if __name__ == "__main__":
    main()
