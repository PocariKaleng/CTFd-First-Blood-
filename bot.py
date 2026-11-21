#!/usr/bin/env python3
"""Bot notifikasi CTFd -> Discord: First Blood, challenge baru, hint baru.

    python bot.py         menu interaktif
    python bot.py --run   langsung start tanpa menu (nohup / Docker / systemd)
"""

import argparse
import copy
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Modul 'requests' belum terpasang. Jalankan: pip install -r requirements.txt")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"

# Urutan di sini = urutan proses dalam satu siklus polling.
KINDS = ("challenge", "hint", "firstblood")
FEATURE_ORDER = ("firstblood", "challenge", "hint")  # urutan tampilan di menu
KIND_LABEL = {"firstblood": "First Blood", "challenge": "Challenge Baru", "hint": "Hint Baru"}
STATE_LABEL = {"firstblood": "first blood", "challenge": "challenge", "hint": "hint"}

COLOR_FIRSTBLOOD = 0xE74C3C
COLOR_CHALLENGE = 0x2ECC71
COLOR_HINT = 0xF1C40F
COLOR_MANUAL = 0x3498DB
COLOR_FROZEN = 0x4F545C

FROZEN_TITLE = "🩸 First Blood! ❓ Anonymous solved ❓ Anonymous Challenge (❓ Anonymous)"
FROZEN_FOOTER = "Scoreboard frozen — details will be revealed later"
REVEAL_TITLE = "🔓 Freeze First Bloods Revealed"
REVEAL_LIMIT = 3800  # batas deskripsi embed Discord 4096, sisakan ruang

DEFAULT_TZ = "Asia/Jakarta"
# Zona Indonesia tidak punya DST, jadi cukup offset tetap (tidak butuh paket tzdata di Windows).
TZ_FIXED = {
    "Asia/Jakarta": (7, "WIB"), "Asia/Pontianak": (7, "WIB"), "Asia/Makassar": (8, "WITA"),
    "Asia/Jayapura": (9, "WIT"), "UTC": (0, "UTC"), "Etc/UTC": (0, "UTC"),
}

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+/?(?:\?\S*)?$"
)
HTTP_TIMEOUT = 15
FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- tampilan

USE_COLOR = False
TAG_COLORS = {
    "FIRSTBLOOD": "31", "NEWCHAL": "32", "HINT": "33", "ANNOUNCE": "34",
    "DRY-RUN": "95", "INFO": "36", "ERROR": "91", "FROZEN": "1;37", "REVEAL": "35",
}


def setup_console():
    global USE_COLOR
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # nama tim aneh tidak bikin crash
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        os.system("")  # aktifkan warna ANSI di console Windows
    USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else str(text)


def red(text):
    return paint(text, "31")


def green(text):
    return paint(text, "32")


def yellow(text):
    return paint(text, "33")


def bold(text):
    return paint(text, "1")


def dim(text):
    return paint(text, "2")


def onoff(flag):
    return green("[ON] ") if flag else red("[OFF]")


def log(tag, msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {paint('[' + tag + ']', TAG_COLORS.get(tag, '0'))} {msg}", flush=True)


# ---------------------------------------------------------------- file JSON

def read_json(path):
    """None kalau file belum ada. Raise OSError/ValueError kalau rusak."""
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path, data, private=False):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if private and os.name != "nt":
        os.chmod(tmp, 0o600)  # config berisi token
    os.replace(tmp, path)


# ---------------------------------------------------------------- config

FIELDS = [
    ("ctfd_url", "CTFd URL"),
    ("token", "Admin API token"),
    ("webhook_main", "Webhook utama"),
    ("webhook_firstblood", "Webhook First Blood (kosong = pakai utama)"),
    ("webhook_challenge", "Webhook Challenge Baru (kosong = pakai utama)"),
    ("webhook_hint", "Webhook Hint (kosong = pakai utama)"),
    ("interval", "Interval polling (detik, default 15)"),
    ("show_hint_content", "Tampilkan isi hint di announcement (y/N)"),
]
FIELD_LABEL = dict(FIELDS)
REQUIRED = {"ctfd_url", "token", "webhook_main"}
OPTIONAL_WEBHOOKS = {"webhook_firstblood", "webhook_challenge", "webhook_hint"}


def default_config():
    return {
        "ctfd_url": "",
        "token": "",
        "webhook_main": "",
        "webhook_firstblood": "",
        "webhook_challenge": "",
        "webhook_hint": "",
        "interval": 15,
        "show_hint_content": False,
        "features": {kind: True for kind in FEATURE_ORDER},
        "freeze_manual": "",
        "timezone": DEFAULT_TZ,
        "auto_reveal": False,
    }


def load_config():
    """Return config, atau None kalau config.json belum ada / tidak bisa dibaca."""
    try:
        raw = read_json(CONFIG_FILE)
    except (OSError, ValueError) as e:
        print(red(f"config.json tidak bisa dibaca: {e}"))
        return None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        print(red("config.json tidak valid."))
        return None
    cfg = default_config()
    for key in cfg:
        if key in raw and key != "features":
            cfg[key] = raw[key]
    if isinstance(raw.get("features"), dict):
        for kind in FEATURE_ORDER:
            if kind in raw["features"]:
                cfg["features"][kind] = bool(raw["features"][kind])
    return cfg


def save_config(cfg):
    write_json_atomic(CONFIG_FILE, cfg, private=True)


def mask(value):
    value = str(value or "")
    if len(value) <= 8:
        return "****" if value else ""
    return f"{value[:4]}****{value[-4:]}"


def clean_url(text):
    text = str(text or "").strip()
    if not re.match(r"^https?://[^\s/]+", text, re.IGNORECASE):
        return None
    return text.rstrip("/")


def is_webhook(text):
    return bool(WEBHOOK_RE.match(str(text or "").strip()))


def parse_interval(value):
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    return number if number >= 5 else None


def parse_yes_no(text):
    text = str(text).strip().lower()
    if text in ("y", "yes", "ya"):
        return True
    if text in ("n", "no", "tidak", "t"):
        return False
    return None


def config_problems(cfg, need_webhook=True):
    problems = []
    if not clean_url(cfg.get("ctfd_url")):
        problems.append("CTFd URL belum diisi / tidak valid")
    if not cfg.get("token"):
        problems.append("Admin API token belum diisi")
    if need_webhook and not is_webhook(cfg.get("webhook_main")):
        problems.append("Webhook utama belum diisi / bukan URL Discord webhook")
    for key in sorted(OPTIONAL_WEBHOOKS):
        if cfg.get(key) and not is_webhook(cfg[key]):
            problems.append(f"{FIELD_LABEL[key].split(' (')[0]} bukan URL Discord webhook")
    if parse_interval(cfg.get("interval")) is None:
        problems.append("Interval polling harus angka >= 5")
    if get_tz(cfg.get("timezone")) is None:
        problems.append(f"Timezone '{cfg.get('timezone')}' tidak dikenal (contoh: Asia/Jakarta atau +07:00)")
    elif cfg.get("freeze_manual"):
        try:
            manual_freeze_ts(cfg)
        except ValueError:
            problems.append("Freeze manual harus format YYYY-MM-DD HH:MM")
    return problems


# ---------------------------------------------------------------- waktu & freeze

def get_tz(name):
    """tzinfo dari nama IANA (Asia/Jakarta) atau offset (+07:00). None kalau tidak dikenal."""
    name = str(name or "").strip() or DEFAULT_TZ
    if name in TZ_FIXED:
        hours, abbr = TZ_FIXED[name]
        return timezone(timedelta(hours=hours), abbr)
    m = re.match(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d\d))?$", name.replace(" ", ""), re.IGNORECASE)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        if delta > timedelta(hours=14):
            return None
        return timezone(delta if m.group(1) == "+" else -delta)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # Python 3.8, Windows tanpa paket tzdata, atau nama salah
        return None


def fmt_dt(dt, cfg):
    local = dt.astimezone(get_tz(cfg.get("timezone")) or timezone.utc)
    return f"{local:%Y-%m-%d %H:%M:%S} {local.tzname() or ''}".strip()


def fmt_ts(ts, cfg):
    return fmt_dt(datetime.fromtimestamp(ts, timezone.utc), cfg)


def parse_unix(value):
    """Nilai config CTFd (freeze/end) -> unix timestamp, None kalau kosong."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def manual_freeze_ts(cfg):
    """Timestamp freeze manual, None kalau tidak diset. ValueError kalau format/timezone salah."""
    text = str(cfg.get("freeze_manual") or "").strip()
    if not text:
        return None
    tz = get_tz(cfg.get("timezone"))
    if tz is None:
        raise ValueError(f"timezone '{cfg.get('timezone')}' tidak dikenal")
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()


def current_freeze(api, cfg):
    """(timestamp, sumber). Freeze manual menang dari nilai CTFd."""
    manual = manual_freeze_ts(cfg)
    if manual is not None:
        return manual, "manual"
    ts = parse_unix(api.config("freeze"))
    return (ts, "ctfd") if ts else (None, None)


def is_frozen_solve(solve, freeze_ts):
    """Disensor kalau waktu SOLVE >= freeze. Tanggal tidak terbaca dianggap frozen (aman)."""
    if freeze_ts is None:
        return False
    when = parse_date(solve.get("date"))
    return when is None or when.timestamp() >= freeze_ts


# ---------------------------------------------------------------- state

def load_state():
    """Return (state, kategori_yang_belum_ada). state = {kind: set(id)}."""
    try:
        raw = read_json(STATE_FILE)
    except (OSError, ValueError) as e:
        log("ERROR", f"state.json tidak bisa dibaca ({e}), dianggap kosong")
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    state, missing = {}, set()
    for kind in KINDS:
        value = raw.get(kind)
        if isinstance(value, list):
            try:
                state[kind] = {int(x) for x in value}
                continue
            except (TypeError, ValueError):
                pass
        state[kind] = set()
        missing.add(kind)
    frozen = raw.get("frozen_bloods")
    state["frozen_bloods"] = [dict(e) for e in frozen if isinstance(e, dict)] if isinstance(frozen, list) else []
    return state, missing


def dump_state(state, skip=()):
    # Kategori yang belum selesai baseline tidak ditulis, supaya kalau bot mati
    # di tengah jalan, start berikutnya tetap bertanya dan tidak spam data lama.
    data = {kind: sorted(state[kind]) for kind in KINDS if kind not in skip}
    if state.get("frozen_bloods"):
        data["frozen_bloods"] = [dict(e) for e in state["frozen_bloods"]]  # salinan, bukan referensi
    return data


def frozen_key(entry):
    return (entry.get("challenge_id"), entry.get("account_id"), entry.get("date"))


def pending_reveal(state):
    return [e for e in state.get("frozen_bloods", []) if not e.get("revealed")]


def merge_revealed_from_disk(state):
    """Ambil tanda 'revealed' dari state.json, misalnya hasil reveal manual dari menu di
    proses lain, supaya bot yang sedang jalan tidak menimpanya lalu reveal dua kali."""
    try:
        raw = read_json(STATE_FILE)
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict) or not isinstance(raw.get("frozen_bloods"), list):
        return
    done = {frozen_key(e) for e in raw["frozen_bloods"] if isinstance(e, dict) and e.get("revealed")}
    for entry in state.get("frozen_bloods", []):
        if frozen_key(entry) in done:
            entry["revealed"] = True


def mark_revealed_on_disk(entries):
    keys = {frozen_key(e) for e in entries}
    raw = read_json(STATE_FILE)
    if not isinstance(raw, dict):
        return
    for entry in raw.get("frozen_bloods") or []:
        if isinstance(entry, dict) and frozen_key(entry) in keys:
            entry["revealed"] = True
    write_json_atomic(STATE_FILE, raw)


def persist_state(state, skip, last_saved):
    merge_revealed_from_disk(state)
    current = dump_state(state, skip)
    if current == last_saved:
        return last_saved
    try:
        write_json_atomic(STATE_FILE, current)
    except OSError as e:
        log("ERROR", f"Gagal menyimpan state.json: {e}")
        return last_saved
    return current


def reset_state(kinds):
    if set(kinds) == set(KINDS):
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        return
    try:
        raw = read_json(STATE_FILE)
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return
    for kind in kinds:
        raw.pop(kind, None)
    write_json_atomic(STATE_FILE, raw)


def state_summary():
    if not STATE_FILE.exists():
        return "belum ada"
    state, missing = load_state()
    parts = [f"{len(state[k])} {STATE_LABEL[k]}" if k not in missing else f"{STATE_LABEL[k]} kosong"
             for k in KINDS]
    if state["frozen_bloods"]:
        parts.append(f"{len(state['frozen_bloods'])} frozen blood ({len(pending_reveal(state))} belum di-reveal)")
    return ", ".join(parts)


# ---------------------------------------------------------------- CTFd API

class AuthError(Exception):
    """Token ditolak CTFd (401/403): bot berhenti."""


class CTFdError(Exception):
    """Error jaringan / CTFd: dicoba lagi di siklus berikutnya."""


class CTFd:
    def __init__(self, url, token):
        self.base = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "User-Agent": "ctfd-discord-bot",
        })

    def get(self, path, params=None, allow_404=False):
        try:
            r = self.session.get(self.base + path, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise CTFdError(f"Gagal konek ke CTFd ({path}): {e}") from None
        redirected_to_login = bool(r.history) and "/login" in r.url
        if r.status_code in (401, 403) or redirected_to_login:
            reason = "dialihkan ke /login" if redirected_to_login else f"HTTP {r.status_code}"
            raise AuthError(
                f"CTFd menolak token ({reason} di {path}). "
                "Pastikan token benar, belum expired, dan milik akun admin."
            )
        if r.status_code == 404 and allow_404:
            return {}
        if r.status_code != 200:
            raise CTFdError(f"CTFd membalas HTTP {r.status_code} untuk {path}")
        try:
            data = r.json()
        except ValueError:
            raise CTFdError(f"Respons {path} bukan JSON. Cek CTFd URL.") from None
        if not isinstance(data, dict) or data.get("success") is False:
            raise CTFdError(f"Respons {path} tidak valid: {str(data)[:200]}")
        return data

    def get_all(self, path, params=None):
        """GET list, ikuti meta.pagination kalau ada."""
        params = dict(params or {})
        items = []
        for _ in range(1000):
            data = self.get(path, params)
            chunk = data.get("data") or []
            if not isinstance(chunk, list):
                raise CTFdError(f"Respons {path} bukan list")
            items.extend(chunk)
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            pagination = meta.get("pagination") or {}
            next_page = pagination.get("next")
            if not next_page or str(next_page) == str(pagination.get("page")):
                break
            params["page"] = next_page
        return items

    def challenges(self):
        # Tanpa view=admin: hanya challenge yang visible.
        return self.get_all("/api/v1/challenges")

    def hints(self):
        return self.get_all("/api/v1/hints")

    def solves(self, challenge_id):
        return self.get_all(f"/api/v1/challenges/{challenge_id}/solves")

    def hint_content(self, hint_id):
        # List /hints tidak menyertakan isi hint; admin perlu ?preview=true.
        data = self.get(f"/api/v1/hints/{hint_id}", {"preview": "true"}).get("data")
        return (data.get("content") or "") if isinstance(data, dict) else ""

    def config(self, key):
        # /api/v1/configs/<key> (admin): data berisi nilai mentah, mis. unix timestamp atau null.
        return self.get(f"/api/v1/configs/{key}", allow_404=True).get("data")


# ---------------------------------------------------------------- Discord

DISCORD = requests.Session()  # terpisah dari session CTFd: token CTFd tidak ikut terkirim


def float_or(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def retry_after(response):
    try:
        wait = float(response.json().get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        wait = float_or(response.headers.get("Retry-After"), 1.0)
    return min(max(wait, 0.5), 60.0)


def send_webhook(url, payload):
    secret = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    for _ in range(5):
        try:
            r = DISCORD.post(url, json=payload, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            log("ERROR", f"Gagal kirim ke Discord: {str(e).replace(secret, '****')}")
            return False
        if r.status_code == 429:
            wait = retry_after(r)
            log("INFO", f"Kena rate limit Discord, tunggu {wait:.1f} detik")
            time.sleep(wait)
            continue
        if r.ok:
            if r.headers.get("X-RateLimit-Remaining") == "0":
                time.sleep(min(float_or(r.headers.get("X-RateLimit-Reset-After"), 1.0), 60.0))
            return True
        log("ERROR", f"Discord membalas HTTP {r.status_code}: {r.text[:200]}")
        return False
    log("ERROR", "Discord masih rate limit setelah 5x coba, dicoba lagi nanti")
    return False


def clip(value, limit=1024):
    text = "" if value is None else str(value).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "-"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$", text)
    if not m:
        return None
    frac = "." + (m.group(2) + "000000")[:6] if m.group(2) else ""
    try:
        dt = datetime.fromisoformat(m.group(1) + frac + m.group(3))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def solve_key(solve):
    return (parse_date(solve.get("date")) or FAR_FUTURE, str(solve.get("date") or ""))


def embed_firstblood(chal, solve, cfg):
    when = parse_date(solve.get("date"))
    solved_at = fmt_dt(when, cfg) if when else clip(solve.get("date"))
    embed = {
        "title": clip(f"🩸 First Blood! {solve.get('name')} solved {chal.get('name')} ({chal.get('category')})", 256),
        "color": COLOR_FIRSTBLOOD,
        "fields": [{"name": "Solved at", "value": solved_at}],
    }
    if when:
        embed["timestamp"] = when.isoformat()
    return embed


def embed_firstblood_frozen():
    # Sengaja statis: tidak ada nama solver, challenge, category, value, maupun waktu.
    return {"title": FROZEN_TITLE, "color": COLOR_FROZEN, "footer": {"text": FROZEN_FOOTER}}


def md_escape(text):
    return re.sub(r"([\\*_~`|>])", r"\\\1", str(text))


def reveal_line(entry, cfg):
    when = parse_date(entry.get("date"))
    stamp = fmt_dt(when, cfg) if when else clip(entry.get("date"))
    return (f"{md_escape(clip(entry.get('challenge'), 200))} ({md_escape(clip(entry.get('category'), 100))})"
            f" — 🩸 {md_escape(clip(entry.get('solver'), 200))} — {stamp}")


def reveal_embeds(entries, cfg, limit=REVEAL_LIMIT):
    """Daftar reveal urut waktu solve, dipecah per embed: [(entries, embed), ...]."""
    groups, size = [], limit + 1
    for entry in sorted(entries, key=solve_key):
        line = reveal_line(entry, cfg)
        if size + len(line) + 1 > limit:
            groups.append([])
            size = 0
        groups[-1].append((entry, line))
        size += len(line) + 1
    result = []
    for i, group in enumerate(groups, 1):
        title = REVEAL_TITLE if len(groups) == 1 else f"{REVEAL_TITLE} ({i}/{len(groups)})"
        embed = {
            "title": title,
            "description": "\n".join(line for _, line in group),
            "color": COLOR_FIRSTBLOOD,
            "timestamp": now_iso(),
        }
        result.append(([entry for entry, _ in group], embed))
    return result


def send_reveal(entries, cfg, notify):
    """Kirim reveal per embed. Entry ditandai revealed hanya kalau embed-nya terkirim."""
    sent = []
    for group, embed in reveal_embeds(entries, cfg):
        if not notify("firstblood", embed):
            break
        for entry in group:
            entry["revealed"] = True
        sent.extend(group)
    return sent


def embed_challenge(chal):
    return {
        "title": clip(f"🆕 New Challenge: {chal.get('name')}", 256),
        "color": COLOR_CHALLENGE,
        "fields": [
            {"name": "Category", "value": clip(chal.get("category")), "inline": True},
            {"name": "Value", "value": clip(chal.get("value")), "inline": True},
        ],
        "timestamp": now_iso(),
    }


def embed_hint(hint, chal, content=""):
    cost = hint.get("cost") or 0
    fields = [{"name": "Cost", "value": f"{cost} points" if cost else "Free", "inline": True}]
    if content:
        fields.append({"name": "Hint", "value": clip(content)})
    return {
        "title": clip(f"💡 New Hint for {chal.get('name')}", 256),
        "color": COLOR_HINT,
        "fields": fields,
        "timestamp": now_iso(),
    }


def make_notifier(cfg, dry_run):
    def notify(kind, embed):
        if dry_run:
            log("DRY-RUN", f"-> webhook {kind}: {embed['title']}")
            for line in str(embed.get("description") or "").splitlines():
                print(f"{'':11}{line}", flush=True)
            for field in embed.get("fields", []):
                print(f"{'':11}{field['name']}: {field['value']}", flush=True)
            if embed.get("footer"):
                print(f"{'':11}({embed['footer']['text']})", flush=True)
            return True
        url = cfg.get(f"webhook_{kind}") or cfg["webhook_main"]
        return send_webhook(url, {"embeds": [embed], "allowed_mentions": {"parse": []}})
    return notify


# ---------------------------------------------------------------- polling

def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def add_frozen(state, cid, chal, solve):
    entry = {
        "challenge_id": cid,
        "challenge": chal.get("name"),
        "category": chal.get("category"),
        "solver": solve.get("name"),
        "account_id": solve.get("account_id"),
        "date": solve.get("date"),
        "revealed": False,
    }
    if all(frozen_key(e) != frozen_key(entry) for e in state["frozen_bloods"]):
        state["frozen_bloods"].append(entry)


def run_cycle(api, cfg, state, notify, quiet=()):
    """Satu siklus polling: challenge baru -> hint baru -> first blood -> auto-reveal.

    quiet  : kategori yang dicatat diam-diam (data lama saat state belum ada).
    Fitur yang dimatikan tetap mencatat ke state tanpa mengirim.
    Item masuk state setelah terkirim; kalau gagal kirim, dicoba lagi siklus berikutnya.
    """
    features = cfg["features"]
    state.setdefault("frozen_bloods", [])

    def note(kind):
        return "" if features.get(kind, True) else dim(" (fitur nonaktif, hanya dicatat)")

    visible = {}
    for chal in api.challenges():
        cid = to_int(chal.get("id")) if isinstance(chal, dict) else None
        # type "hidden" = challenge terkunci prerequisite yang ditampilkan sebagai "???"
        if cid is not None and chal.get("type") != "hidden":
            visible[cid] = chal

    # 1. Challenge baru
    newly_visible = set()
    for cid in sorted(visible):
        if cid in state["challenge"]:
            continue
        chal = visible[cid]
        newly_visible.add(cid)
        if "challenge" not in quiet:
            log("NEWCHAL", f"{chal.get('name')} [{chal.get('category')}, {chal.get('value')} pts]" + note("challenge"))
            if features.get("challenge", True) and not notify("challenge", embed_challenge(chal)):
                continue
        state["challenge"].add(cid)

    # 2. Hint baru, hanya untuk challenge yang visible. Hint yang sudah ada saat
    #    challenge-nya baru muncul dianggap bagian dari rilis challenge itu.
    hints = [h for h in api.hints() if isinstance(h, dict) and to_int(h.get("id")) is not None]
    for hint in sorted(hints, key=lambda h: to_int(h["id"])):
        hid = to_int(hint["id"])
        cid = to_int(hint.get("challenge_id", hint.get("challenge")))
        if hid in state["hint"] or cid not in visible:
            continue
        if "hint" not in quiet and cid not in newly_visible:
            chal = visible[cid]
            log("HINT", f"{chal.get('name')} (cost {hint.get('cost') or 0})" + note("hint"))
            if features.get("hint", True):
                content = ""
                if cfg.get("show_hint_content"):
                    content = hint.get("content") or ""
                    if not content:
                        try:
                            content = api.hint_content(hid)
                        except AuthError as e:
                            log("ERROR", f"Isi hint #{hid} tidak bisa diambil: {e}")
                if not notify("hint", embed_hint(hint, chal, content)):
                    continue
        state["hint"].add(hid)

    # 3. First blood, hanya poll challenge yang belum punya first blood.
    #    Freeze dibaca ulang tiap siklus; sensor ditentukan dari waktu solve.
    freeze_ts, _ = current_freeze(api, cfg)
    for cid in sorted(visible):
        if cid in state["firstblood"]:
            continue
        solves = [s for s in api.solves(cid) if isinstance(s, dict)]
        if not solves:
            continue
        first = min(solves, key=solve_key)
        chal = visible[cid]
        if "firstblood" not in quiet:
            frozen = is_frozen_solve(first, freeze_ts)
            send = features.get("firstblood", True)
            message = f"{first.get('name')} solved {chal.get('name')} [{chal.get('category')}]" + note("firstblood")
            log("FROZEN" if frozen else "FIRSTBLOOD", message + (" (disensor di Discord)" if frozen and send else ""))
            if send:
                embed = embed_firstblood_frozen() if frozen else embed_firstblood(chal, first, cfg)
                if not notify("firstblood", embed):
                    continue
                if frozen:
                    add_frozen(state, cid, chal, first)
        state["firstblood"].add(cid)

    # 4. Auto-reveal: saat freeze dihapus, atau waktu end CTF sudah lewat.
    pending = pending_reveal(state)
    if cfg.get("auto_reveal") and pending:
        reason = None
        if freeze_ts is None:
            reason = "freeze sudah dihapus"
        else:
            end_ts = parse_unix(api.config("end"))
            if end_ts and time.time() >= end_ts:
                reason = "waktu end CTF sudah lewat"
        if reason:
            sent = send_reveal(pending, cfg, notify)
            log("REVEAL", f"Auto-reveal ({reason}): {len(sent)}/{len(pending)} first blood di-reveal")


def ask_yes_no(prompt, default=False):
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        answer = parse_yes_no(raw)
        if answer is not None:
            return answer
        print(red("Pilihan tidak valid"))


def ask_announce_existing(missing):
    """Return kategori yang dicatat diam-diam (kosong = announce semua)."""
    if not sys.stdin.isatty():
        log("INFO", "State belum ada: data yang sudah ada dicatat tanpa announce.")
        return missing
    if set(missing) == set(KINDS):
        print("state.json belum ada.")
    else:
        names = ", ".join(STATE_LABEL[k] for k in KINDS if k in missing)
        print(f"State {names} kosong (habis di-reset).")
    if ask_yes_no("Announce data yang sudah ada? (y/N): ", default=False):
        return set()
    return set(missing)


def run_bot(cfg, dry_run=False):
    """Loop polling. Return False kalau berhenti karena config/token bermasalah."""
    problems = config_problems(cfg, need_webhook=not dry_run)
    if problems:
        print(red("Konfigurasi belum lengkap:"))
        for problem in problems:
            print(f"  - {problem}")
        print("Perbaiki lewat menu 3 (Setup).")
        return False

    api = CTFd(clean_url(cfg["ctfd_url"]), cfg["token"])
    notify = make_notifier(cfg, dry_run)
    interval = parse_interval(cfg["interval"])
    state, missing = load_state()
    last_saved = dump_state(state, missing)
    quiet = ask_announce_existing(missing) if missing else set()

    features = "  ".join(f"{onoff(cfg['features'][k])} {KIND_LABEL[k]}" for k in FEATURE_ORDER)
    log("INFO", f"Bot jalan: {cfg['ctfd_url']} tiap {interval} detik. Ctrl+C untuk berhenti.")
    log("INFO", f"Fitur: {features}")
    if dry_run:
        log("INFO", "Mode dry-run: tidak ada yang dikirim ke Discord, state.json tidak diubah.")

    try:
        while True:
            try:
                if not dry_run:
                    merge_revealed_from_disk(state)
                run_cycle(api, cfg, state, notify, quiet)
                if quiet:
                    counts = ", ".join(f"{len(state[k])} {STATE_LABEL[k]}" for k in KINDS if k in quiet)
                    log("INFO", f"Data lama dicatat tanpa announce: {counts}")
                    quiet = set()
            except AuthError as e:
                log("ERROR", str(e))
                print(red("Token CTFd ditolak. Cek token lewat menu 3 lalu coba lagi."))
                return False
            except CTFdError as e:
                log("ERROR", f"{e}. Dicoba lagi {interval} detik lagi.")
            except Exception as e:  # data aneh dari server: jangan sampai bot mati
                log("ERROR", f"Error tak terduga: {e!r}. Dicoba lagi {interval} detik lagi.")
            finally:
                if not dry_run:
                    last_saved = persist_state(state, quiet, last_saved)
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        log("INFO", "Bot dihentikan.")
        return True


# ---------------------------------------------------------------- menu

def show_value(cfg, key):
    value = cfg.get(key)
    if key in OPTIONAL_WEBHOOKS and not value:
        return "(kosong, pakai webhook utama)"
    if key == "token" or key.startswith("webhook_"):
        return mask(value) or "(kosong)"
    if key == "show_hint_content":
        return "ya" if value else "tidak"
    if key == "interval":
        return f"{value} detik"
    return str(value) if value else "(kosong)"


FIELD_HELP = {
    "ctfd_url": [
        "Alamat website CTF kamu (yang dibuka peserta).",
        "Contoh: https://ctf.kampus.ac.id",
    ],
    "token": [
        "Token dari akun ADMIN CTFd, supaya bot bisa membaca challenge, solve, dan hint.",
        "Cara ambil: login admin di CTFd -> Settings -> Access Tokens -> Generate.",
        "Biasanya diawali ctfd_",
    ],
    "webhook_main": [
        "Link webhook channel Discord tempat semua pengumuman dikirim.",
        "Cara bikin: di Discord buka Edit Channel -> Integrations -> Webhooks,",
        "klik New Webhook, lalu Copy Webhook URL.",
        "Bentuknya: https://discord.com/api/webhooks/...",
    ],
    "webhook_firstblood": [
        "Isi kalau First Blood mau dikirim ke channel lain.",
        "Kalau tidak, cukup tekan Enter (ikut webhook utama).",
    ],
    "webhook_challenge": [
        "Isi kalau pengumuman challenge baru mau dikirim ke channel lain.",
        "Kalau tidak, cukup tekan Enter (ikut webhook utama).",
    ],
    "webhook_hint": [
        "Isi kalau pengumuman hint baru mau dikirim ke channel lain.",
        "Kalau tidak, cukup tekan Enter (ikut webhook utama).",
    ],
    "interval": [
        "Seberapa sering bot mengecek CTFd, dalam detik (minimal 5).",
        "Kalau ragu, tekan Enter saja.",
    ],
    "show_hint_content": [
        "Tulis juga isi hint-nya di pengumuman Discord?",
        "Hati-hati: hint berbayar jadi bisa dibaca gratis oleh semua orang.",
    ],
}

# Halaman CTFd yang sering ikut ter-copy dari address bar browser.
CTFD_PAGE_RE = re.compile(
    r"/(api|challenges|scoreboard|login|register|users|teams|notifications|settings|admin)(/.*)?$",
    re.IGNORECASE,
)


def strip_ctfd_page(url):
    """https://ctf.x/challenges#a -> (https://ctf.x, '/challenges')."""
    url = url.split("#")[0].split("?")[0].rstrip("/")
    m = re.match(r"^(https?://[^/]+)(/.*)?$", url, re.IGNORECASE)
    path = m.group(2) or ""
    page = CTFD_PAGE_RE.search(path)
    if not page:
        return url, None
    return (m.group(1) + path[:page.start()]).rstrip("/"), path[page.start():]


def validate(key, raw):
    """Return (nilai, pesan_error, catatan)."""
    raw = raw.strip().strip("\"'").strip()
    if key == "ctfd_url":
        url = clean_url(raw)
        if not url:
            return None, "Alamat harus diawali http:// atau https://, contoh: https://ctf.kampus.ac.id", None
        base, removed = strip_ctfd_page(url)
        note = f"Bagian '{removed}' dibuang, yang dipakai alamat utamanya: {base}" if removed else None
        return base, None, note
    if key == "token":
        token = re.sub(r"^(token|bearer)\s+", "", raw, flags=re.IGNORECASE)
        if re.search(r"\s", token):
            return None, "Token tidak boleh ada spasi. Coba salin ulang tokennya.", None
        return token, None, None
    if key.startswith("webhook_"):
        if is_webhook(raw):
            return raw, None, None
        return None, "Itu bukan link webhook Discord. Bentuknya: https://discord.com/api/webhooks/angka/kode", None
    if key == "interval":
        number = parse_interval(raw)
        return (number, None, None) if number else (None, "Isi angka saja, minimal 5. Contoh: 15", None)
    if key == "show_hint_content":
        answer = parse_yes_no(raw)
        return (answer, None, None) if answer is not None else (None, "Ketik y (ya) atau n (tidak).", None)
    return raw, None, None


def enter_hint(cfg, key):
    """Penjelasan apa yang terjadi kalau user langsung tekan Enter."""
    value = cfg.get(key)
    if key in OPTIONAL_WEBHOOKS:
        if value:
            return "Enter = tetap pakai yang sekarang. Ketik - untuk menghapus (balik ikut webhook utama)."
        return "Enter = lewati (ikut webhook utama)."
    if key == "interval":
        return f"Enter = tetap {value} detik."
    if key == "show_hint_content":
        return f"Ketik y (ya) atau n (tidak). Enter = tetap {'ya' if value else 'tidak'}."
    if value:
        return "Enter = tetap pakai yang sekarang, atau ketik yang baru."
    return "Ketik atau paste, lalu tekan Enter."


def edit_field(cfg, key, step=None):
    """Tanyakan satu isian. Return True kalau nilainya diubah."""
    label = FIELD_LABEL[key].split(" (")[0]
    prefix = f"[{step}/{len(FIELDS)}] " if step else ""
    tag = "wajib" if key in REQUIRED else "boleh dilewati"
    print(f"\n{bold(prefix + label)}  {dim('(' + tag + ')')}")
    for line in FIELD_HELP[key]:
        print(f"  {line}")
    if cfg.get(key) or key in ("interval", "show_hint_content"):
        print(f"  Sekarang: {show_value(cfg, key)}")
    print(dim(f"  {enter_hint(cfg, key)}"))
    while True:
        raw = input("  > ").strip()
        if not raw:
            if key in REQUIRED and not cfg.get(key):
                print(red("  Bagian ini wajib diisi."))
                continue
            return False
        if raw == "-" and key in OPTIONAL_WEBHOOKS:
            if not cfg.get(key):
                return False
            cfg[key] = ""
            print(dim("  Dihapus, sekarang ikut webhook utama."))
            return True
        value, error, note = validate(key, raw)
        if error:
            print(red(f"  {error}"))
            continue
        if note:
            print(yellow(f"  {note}"))
        cfg[key] = value
        return True


def wizard(cfg):
    print(bold("\nSetup bot"))
    print("Yang wajib diisi cuma 3: URL CTFd, token admin, dan webhook Discord.")
    print("Sisanya boleh dilewati dengan menekan Enter.")
    print(dim("Semua isian bisa diubah lagi nanti lewat menu 3. Ctrl+C untuk batal."))
    draft = copy.deepcopy(cfg)  # kalau dibatalkan di tengah, config lama tetap utuh
    for step, (key, _) in enumerate(FIELDS, 1):
        edit_field(draft, key, step)
    cfg.clear()
    cfg.update(draft)
    save_config(cfg)
    print(green(f"\nSelesai! Pengaturan disimpan ke {CONFIG_FILE.name} (satu folder dengan bot.py)."))
    if ask_yes_no("Tes koneksi ke CTFd dan Discord sekarang? (Y/n): ", default=True):
        test_connection(cfg)


def setup_menu(cfg):
    while True:
        print(bold("\nSetup / ubah konfigurasi"))
        for i, (key, label) in enumerate(FIELDS, 1):
            print(f"  {i}. {label}  {dim('[' + show_value(cfg, key) + ']')}")
        print("  9. Isi semua dari awal (wizard)")
        print("  0. Kembali")
        choice = input("Pilih: ").strip()
        if choice == "0":
            return
        if choice == "9":
            wizard(cfg)
        elif choice in [str(i) for i in range(1, len(FIELDS) + 1)]:
            if edit_field(cfg, FIELDS[int(choice) - 1][0]):
                save_config(cfg)
                print(green("  Tersimpan."))
        else:
            print(red("Pilihan tidak valid"))


def show_config(cfg):
    print(bold("\nKonfigurasi sekarang") + dim(f"  ({CONFIG_FILE})"))
    width = max(len(label.split(" (")[0]) for _, label in FIELDS)
    for key, label in FIELDS:
        print(f"  {label.split(' (')[0]:<{width}} : {show_value(cfg, key)}")
    for kind in FEATURE_ORDER:
        print(f"  {'Fitur ' + KIND_LABEL[kind]:<{width}} : {onoff(cfg['features'][kind])}")
    print(f"  {'Freeze manual':<{width}} : {cfg.get('freeze_manual') or '(tidak diset, pakai nilai CTFd)'}")
    print(f"  {'Timezone':<{width}} : {cfg.get('timezone') or DEFAULT_TZ}")
    print(f"  {'Auto-reveal':<{width}} : {onoff(cfg.get('auto_reveal'))}")
    print(f"  {'State':<{width}} : {state_summary()}")


def test_connection(cfg):
    problems = config_problems(cfg, need_webhook=False)
    if problems:
        print(red("Konfigurasi belum lengkap:"))
        for problem in problems:
            print(f"  - {problem}")
        return
    all_ok = True
    print(bold("\nTes CTFd"))
    api = CTFd(clean_url(cfg["ctfd_url"]), cfg["token"])
    try:
        challenges = [c for c in api.challenges() if isinstance(c, dict) and c.get("type") != "hidden"]
        print(green(f"  OK     token diterima, {len(challenges)} challenge visible"))
        hints = api.hints()
        print(green(f"  OK     akses admin, {len(hints)} hint"))
    except AuthError as e:
        all_ok = False
        print(red(f"  GAGAL  {e}"))
        print(yellow("         Cek token admin lewat menu 3 -> 2."))
    except CTFdError as e:
        all_ok = False
        print(red(f"  GAGAL  {e}"))
        print(yellow("         Cek URL CTFd lewat menu 3 -> 1, dan pastikan website CTFd bisa dibuka."))

    print(bold("Tes Discord"))
    targets = {}
    for key, label in (("webhook_main", "utama"), ("webhook_firstblood", "First Blood"),
                       ("webhook_challenge", "Challenge Baru"), ("webhook_hint", "Hint")):
        if cfg.get(key):
            targets.setdefault(cfg[key], []).append(label)
    if not targets:
        print(yellow("  Belum ada webhook yang diisi."))
        return
    for url, labels in targets.items():
        names = ", ".join(labels)
        embed = {
            "title": "✅ Test koneksi bot CTFd",
            "description": f"Webhook {names} terhubung.",
            "color": COLOR_MANUAL,
            "timestamp": now_iso(),
        }
        if send_webhook(url, {"embeds": [embed]}):
            print(green(f"  OK     webhook {names} ({mask(url)}), cek pesan tes di Discord"))
        else:
            all_ok = False
            print(red(f"  GAGAL  webhook {names} ({mask(url)})"))
            print(yellow("         Cek link webhook lewat menu 3, atau buat webhook baru di Discord."))
    if all_ok:
        print(green("\nSemua beres. Pilih 1 di menu utama untuk start bot."))


def toggle_menu(cfg):
    while True:
        print(bold("\nToggle fitur"))
        for i, kind in enumerate(FEATURE_ORDER, 1):
            print(f"  {i}. {onoff(cfg['features'][kind])} {KIND_LABEL[kind]}")
        print("  0. Kembali")
        choice = input("Pilih: ").strip()
        if choice == "0":
            return
        if choice in ("1", "2", "3"):
            kind = FEATURE_ORDER[int(choice) - 1]
            cfg["features"][kind] = not cfg["features"][kind]
            save_config(cfg)
        else:
            print(red("Pilihan tidak valid"))


def manual_announce(cfg):
    if not is_webhook(cfg.get("webhook_main")):
        print(red("Webhook utama belum diisi. Isi dulu lewat menu 3."))
        return
    print(bold("\nKirim announcement manual"))
    print("Ketik pesan (boleh beberapa baris). Baris kosong = selesai.")
    lines = []
    while True:
        line = input("> ")
        if not line.strip():
            break
        lines.append(line.rstrip())
    if not lines:
        print("Dibatalkan, pesan kosong.")
        return
    if not ask_yes_no("Kirim ke webhook utama? (Y/n): ", default=True):
        print("Dibatalkan.")
        return
    embed = {
        "title": "📢 Announcement",
        "description": clip("\n".join(lines), 4096),
        "color": COLOR_MANUAL,
        "timestamp": now_iso(),
    }
    if send_webhook(cfg["webhook_main"], {"embeds": [embed], "allowed_mentions": {"parse": []}}):
        log("ANNOUNCE", "Terkirim ke webhook utama.")


def freeze_status(cfg):
    try:
        manual_ts = manual_freeze_ts(cfg)
    except ValueError as e:
        manual_ts = None
        print(red(f"Freeze manual tidak valid: {e}"))
    ctfd_ts, ctfd_ok = None, False
    if clean_url(cfg.get("ctfd_url")) and cfg.get("token"):
        try:
            ctfd_ts = parse_unix(CTFd(clean_url(cfg["ctfd_url"]), cfg["token"]).config("freeze"))
            ctfd_ok = True
            ctfd_text = fmt_ts(ctfd_ts, cfg) if ctfd_ts else "(tidak diset)"
        except (AuthError, CTFdError) as e:
            ctfd_text = red(f"gagal dibaca: {e}")
    else:
        ctfd_text = "(CTFd URL / token belum diisi)"

    if manual_ts:
        effective, source = manual_ts, "manual (menang dari nilai CTFd)"
    elif ctfd_ts:
        effective, source = ctfd_ts, "CTFd"
    elif ctfd_ok:
        effective, source = None, "tidak ada (freeze tidak diset)"
    else:
        effective, source = None, "tidak diketahui"

    if effective:
        status = red("FROZEN") if time.time() >= effective else green("NORMAL") + dim(" (freeze belum mulai)")
    elif ctfd_ok:
        status = green("NORMAL")
    else:
        status = yellow("TIDAK DIKETAHUI")
    state, _ = load_state()
    rows = [
        ("Sumber", source),
        ("Waktu freeze", fmt_ts(effective, cfg) if effective else "-"),
        ("Freeze di CTFd", ctfd_text),
        ("Freeze manual", cfg.get("freeze_manual") or "(tidak diset)"),
        ("Timezone", cfg.get("timezone") or DEFAULT_TZ),
        ("Status sekarang", status),
        ("Auto-reveal", onoff(cfg.get("auto_reveal"))),
        ("FB tersensor", f"{len(state['frozen_bloods'])} ({len(pending_reveal(state))} belum di-reveal)"),
    ]
    print(bold("\nStatus freeze"))
    for label, value in rows:
        print(f"  {label:<15} : {value}")


def set_manual_freeze(cfg):
    tz_name = cfg.get("timezone") or DEFAULT_TZ
    if get_tz(tz_name) is None:
        print(red(f"Timezone '{tz_name}' di config.json tidak dikenal. Pakai mis. Asia/Jakarta atau +07:00."))
        return
    tz_label = TZ_FIXED[tz_name][1] if tz_name in TZ_FIXED else tz_name
    print(bold("\nSet freeze time manual"))
    print("  Isi kalau waktu freeze di CTFd kosong atau mau diganti. Nilai ini menang dari CTFd.")
    print(f"  Format: tahun-bulan-tanggal jam:menit ({tz_label}). Contoh: 2026-11-21 20:00")
    print(f"  Sekarang: {cfg.get('freeze_manual') or '(tidak diset)'}")
    print(dim("  Enter = tidak diubah."))
    while True:
        raw = input("  > ").strip()
        if not raw:
            return
        try:
            value = datetime.strptime(raw, "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            print(red("  Format harus YYYY-MM-DD HH:MM, contoh 2026-11-21 20:00"))
            continue
        cfg["freeze_manual"] = value
        save_config(cfg)
        print(green(f"  Tersimpan. Freeze manual: {fmt_ts(manual_freeze_ts(cfg), cfg)}"))
        return


def clear_manual_freeze(cfg):
    if not cfg.get("freeze_manual"):
        print("Freeze manual memang belum diset.")
        return
    cfg["freeze_manual"] = ""
    save_config(cfg)
    print(green("Freeze manual dihapus. Sekarang pakai nilai freeze dari CTFd."))


def preview_reveal(cfg):
    pending = pending_reveal(load_state()[0])
    if not pending:
        print("Tidak ada first blood tersensor yang belum di-reveal.")
        return
    print(dim(f"\nPreview reveal ({len(pending)} first blood, tidak dikirim):"))
    for _, embed in reveal_embeds(pending, cfg):
        print(bold(embed["title"]))
        print(embed["description"])
        print()


def reveal_now(cfg):
    if not is_webhook(cfg.get("webhook_firstblood") or cfg.get("webhook_main")):
        print(red("Webhook belum diisi. Isi dulu lewat menu 3."))
        return
    pending = pending_reveal(load_state()[0])
    if not pending:
        print("Tidak ada first blood tersensor yang belum di-reveal.")
        return
    target = "First Blood" if cfg.get("webhook_firstblood") else "utama"
    if not ask_yes_no(f"Kirim reveal {len(pending)} first blood ke webhook {target}? (y/N): ", default=False):
        print("Dibatalkan.")
        return
    sent = send_reveal(pending, cfg, make_notifier(cfg, dry_run=False))
    if sent:
        mark_revealed_on_disk(sent)
    if len(sent) == len(pending):
        log("REVEAL", f"{len(sent)} first blood di-reveal ke webhook {target}.")
    else:
        print(red(f"Baru {len(sent)} dari {len(pending)} yang terkirim. Sisanya bisa dicoba lagi."))


def freeze_menu(cfg):
    while True:
        print(bold("\nFreeze menu"))
        print("  1. Lihat status freeze")
        print("  2. Set freeze time manual")
        print("  3. Hapus freeze time manual (kembali pakai nilai dari CTFd)")
        print(f"  4. Toggle auto-reveal {onoff(cfg.get('auto_reveal'))}")
        print("  5. Preview reveal (print ke terminal saja)")
        print("  6. Reveal sekarang (kirim ke Discord)")
        print("  0. Kembali")
        choice = input("Pilih: ").strip()
        if choice == "0":
            return
        if choice == "1":
            freeze_status(cfg)
        elif choice == "2":
            set_manual_freeze(cfg)
        elif choice == "3":
            clear_manual_freeze(cfg)
        elif choice == "4":
            cfg["auto_reveal"] = not cfg.get("auto_reveal")
            save_config(cfg)
        elif choice == "5":
            preview_reveal(cfg)
        elif choice == "6":
            reveal_now(cfg)
        else:
            print(red("Pilihan tidak valid"))


RESET_OPTIONS = {
    "1": ("semua", KINDS),
    "2": ("first blood", ("firstblood",)),
    "3": ("challenge", ("challenge",)),
    "4": ("hint", ("hint",)),
    "5": ("frozen bloods", ("frozen_bloods",)),
}


def reset_menu():
    while True:
        print(bold("\nReset state") + dim(f"  ({state_summary()})"))
        print("  1. Reset semua")
        print("  2. Reset first blood saja")
        print("  3. Reset challenge saja")
        print("  4. Reset hint saja")
        print("  5. Reset frozen bloods")
        print("  0. Kembali")
        choice = input("Pilih: ").strip()
        if choice == "0":
            return
        if choice not in RESET_OPTIONS:
            print(red("Pilihan tidak valid"))
            continue
        name, kinds = RESET_OPTIONS[choice]
        if not ask_yes_no(f"Yakin hapus state {name}? (y/N): ", default=False):
            print("Dibatalkan.")
            continue
        try:
            reset_state(kinds)
        except OSError as e:
            print(red(f"Gagal reset state: {e}"))
            continue
        print(green(f"State {name} dihapus."))
        if set(kinds) & set(KINDS):
            print("Saat bot start nanti, kamu akan ditanya apakah data yang sudah ada mau di-announce.")
        return


MAIN_MENU = """
  1. Start bot
  2. Start bot (dry-run, hanya print ke terminal)
  3. Setup / ubah konfigurasi
  4. Lihat konfigurasi sekarang
  5. Test koneksi
  6. Toggle fitur
  7. Kirim announcement manual
  8. Reset state
  9. Freeze menu
  0. Keluar"""


def menu_loop():
    cfg = load_config()
    if cfg is None:
        print("Halo! Pengaturan bot belum ada, isi dulu ya.")
        cfg = default_config()
        wizard(cfg)

    actions = {
        "1": lambda: run_bot(cfg),
        "2": lambda: run_bot(cfg, dry_run=True),
        "3": lambda: setup_menu(cfg),
        "4": lambda: show_config(cfg),
        "5": lambda: test_connection(cfg),
        "6": lambda: toggle_menu(cfg),
        "7": lambda: manual_announce(cfg),
        "8": reset_menu,
        "9": lambda: freeze_menu(cfg),
    }
    while True:
        features = "  ".join(f"{onoff(cfg['features'][k])} {KIND_LABEL[k]}" for k in FEATURE_ORDER)
        print()
        print(bold("CTFd Discord Bot") + "  " + dim(cfg.get("ctfd_url") or "(CTFd URL belum diisi)"))
        print(f"Fitur: {features}")
        print(MAIN_MENU)
        choice = input("Pilih: ").strip()
        if choice == "0":
            print("Sampai jumpa.")
            return
        if choice not in actions:
            print(red("Pilihan tidak valid"))
            continue
        try:
            actions[choice]()
        except KeyboardInterrupt:
            print("\nDibatalkan.")
        except OSError as e:
            print(red(f"Gagal menulis file: {e}"))


def main():
    parser = argparse.ArgumentParser(
        description="Bot notifikasi CTFd -> Discord (First Blood, challenge baru, hint baru)."
    )
    parser.add_argument("--run", action="store_true",
                        help="langsung start bot tanpa menu (untuk nohup/Docker/systemd)")
    args = parser.parse_args()
    setup_console()

    if args.run:
        cfg = load_config()
        if cfg is None:
            print(f"{CONFIG_FILE} belum ada atau tidak valid. Jalankan `python bot.py` dulu untuk setup.",
                  file=sys.stderr)
            sys.exit(1)
        try:
            ok = run_bot(cfg)
        except KeyboardInterrupt:
            ok = True
        sys.exit(0 if ok else 1)

    try:
        menu_loop()
    except (KeyboardInterrupt, EOFError):
        print("\nKeluar.")


if __name__ == "__main__":
    main()
