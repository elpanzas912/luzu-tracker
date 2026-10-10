"""Luzu tracker: qué videos de YouTube tienen pauta en Google Ads Transparency Center.

Usa los endpoints internos del Transparency Center (sin login, sin API key), filtrando
por día y plataforma YouTube, y extrae el videoId de cada creativo.

    python luzu_tracker.py update                   # todo: anuncios + visitas + dashboard
    python luzu_tracker.py run                      # ayer y hoy
    python luzu_tracker.py run --day 2026-10-07
    python luzu_tracker.py backfill --from 2026-09-01 --to 2026-10-07
    python luzu_tracker.py views                    # foto de visitas de hoy (también la hace `run`)
    python luzu_tracker.py report --day 2026-10-07
    python luzu_tracker.py csv --out pauta.csv
    python luzu_tracker.py ui                       # dashboard.html y lo abre

Ojo: Google calcula los días en hora del Pacífico (PT), no en hora argentina.
"""
import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RPC = "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives?authuser=0"
HEADERS = {
    "content-type": "application/x-www-form-urlencoded",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36",
    "origin": "https://adstransparency.google.com",
    "referer": "https://adstransparency.google.com/",
}
REGION_AR = 2032
PLATFORM_YOUTUBE = 5
PAGE_SIZE = 100
DEFAULT_ADVERTISERS = {"AR16478749479831339009": "LUZU TV S.A."}
HERE = Path(__file__).resolve().parent
DB_PATH = str(HERE / "luzu_tracker.db")
MONTHS = {m: i + 1 for i, m in enumerate("ene feb mar abr may jun jul ago sep oct nov dic".split())}

SCHEMA = """
CREATE TABLE IF NOT EXISTS creatives (
    creative_id   TEXT PRIMARY KEY,
    advertiser_id TEXT NOT NULL,
    video_id      TEXT,
    duration      TEXT,
    channel       TEXT,
    first_shown   TEXT,
    last_shown    TEXT
);
CREATE TABLE IF NOT EXISTS sightings (
    day         TEXT NOT NULL,
    creative_id TEXT NOT NULL REFERENCES creatives(creative_id),
    PRIMARY KEY (day, creative_id)
);
CREATE TABLE IF NOT EXISTS videos (
    video_id  TEXT PRIMARY KEY,
    title     TEXT,
    author    TEXT,
    published TEXT
);
CREATE TABLE IF NOT EXISTS view_snapshots (
    video_id TEXT NOT NULL,
    day      TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    views    INTEGER NOT NULL,
    PRIMARY KEY (video_id, taken_at)
);
"""


def migrate(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(videos)")]
    if "published" not in cols:
        db.execute("ALTER TABLE videos ADD COLUMN published TEXT")
    # Versiones anteriores guardaban una sola foto por día; ahora se guardan todas.
    pk = {r[1] for r in db.execute("PRAGMA table_info(view_snapshots)") if r[5]}
    if "day" in pk:
        db.executescript("""
            ALTER TABLE view_snapshots RENAME TO view_snapshots_old;
            CREATE TABLE view_snapshots (
                video_id TEXT NOT NULL, day TEXT NOT NULL, taken_at TEXT NOT NULL,
                views INTEGER NOT NULL, PRIMARY KEY (video_id, taken_at));
            INSERT INTO view_snapshots SELECT video_id, day, taken_at, views FROM view_snapshots_old;
            DROP TABLE view_snapshots_old;
        """)


def http(req, retries=4):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404) or attempt == retries - 1:
                raise
            if e.code == 429:  # rate limit: esperar bastante antes de reintentar
                time.sleep(int(e.headers.get("Retry-After") or 0) or 30 * (attempt + 1))
                continue
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)


class RateLimited(Exception):
    """Google redirige a su página 'sorry' (límite anti-abuso). No se insiste: se reintenta más tarde."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_rpc_opener = urllib.request.build_opener(_NoRedirect)


def rpc_post(data):
    for attempt in range(3):
        try:
            with _rpc_opener.open(urllib.request.Request(RPC, data, HEADERS), timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (302, 429):
                raise RateLimited(f"Google limitó las consultas (HTTP {e.code}); se reintenta en el próximo ciclo") from None
            if e.code < 500 or attempt == 2:
                raise
        time.sleep(2 ** attempt)


def search_day(advertiser_id, day):
    """Todos los creativos de YouTube que el anunciante mostró en `day` (date)."""
    start = int(day.strftime("%Y%m%d"))
    end = int((day + timedelta(days=1)).strftime("%Y%m%d"))
    body = {
        "2": PAGE_SIZE,
        "3": {
            "6": start,
            "7": end,
            "8": [REGION_AR],
            "12": {"1": "", "2": True},
            "13": {"1": [advertiser_id]},
            "14": [PLATFORM_YOUTUBE],
        },
        "7": {"1": 1, "2": 30, "3": REGION_AR},
    }
    while True:
        data = urllib.parse.urlencode({"f.req": json.dumps(body, separators=(",", ":"))}).encode()
        resp = json.loads(rpc_post(data))
        yield from resp.get("1", [])
        token = resp.get("2")
        if not token:
            return
        body["4"] = token
        time.sleep(0.5)


def ts(value):
    """Timestamp del RPC ({"1": segundos}) a ISO UTC."""
    if not value:
        return None
    return datetime.fromtimestamp(int(value["1"]), timezone.utc).isoformat(timespec="seconds")


def preview_info(url):
    """videoId, duración y canal desde el JS de preview del creativo."""
    js = http(urllib.request.Request(url, headers=HEADERS)).decode("utf-8", "replace")
    js = js.replace("\\x27", "'")

    def field(name):
        m = re.search(name + r"'\s*:\s*'([^']*)'", js)
        return m.group(1) if m else None

    return field("video_videoId"), field("videoDuration"), field("channelName")


def fetch_title(video_id):
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        f"https://www.youtube.com/watch?v={video_id}", safe=""
    )
    try:
        j = json.loads(http(urllib.request.Request(url, headers=HEADERS), retries=2))
        return j.get("title"), j.get("author_name")
    except (urllib.error.URLError, ValueError):
        return None, None


def track_day(db, advertiser_id, day):
    seen = new = 0
    for c in search_day(advertiser_id, day):
        creative_id = c["2"]
        seen += 1
        db.execute("INSERT OR IGNORE INTO sightings VALUES (?, ?)", (day.isoformat(), creative_id))
        row = db.execute("SELECT video_id FROM creatives WHERE creative_id=?", (creative_id,)).fetchone()
        if row is None:
            preview = c.get("3", {}).get("1", {}).get("4")
            video_id = duration = channel = None
            if preview:
                try:
                    video_id, duration, channel = preview_info(preview)
                except urllib.error.URLError as e:
                    print(f"  preview fallo {creative_id}: {e}", file=sys.stderr)
                time.sleep(0.4)
            db.execute(
                "INSERT INTO creatives VALUES (?,?,?,?,?,?,?)",
                (creative_id, advertiser_id, video_id, duration, channel, ts(c.get("6")), ts(c.get("7"))),
            )
            new += 1
        else:
            db.execute("UPDATE creatives SET last_shown=? WHERE creative_id=?", (ts(c.get("7")), creative_id))
        db.commit()
    return seen, new


def fill_titles(db):
    ids = [r[0] for r in db.execute(
        "SELECT DISTINCT video_id FROM creatives WHERE video_id IS NOT NULL "
        "AND video_id NOT IN (SELECT video_id FROM videos)")]
    for vid in ids:
        title, author = fetch_title(vid)
        db.execute(
            "INSERT INTO videos (video_id, title, author) VALUES (?,?,?) "
            "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, author=excluded.author",
            (vid, title, author))
        db.commit()
        time.sleep(0.2)
    return len(ids)


def fetch_views(video_id):
    """Visitas y fecha de publicación desde la página de YouTube (sin API key)."""
    req = urllib.request.Request(
        f"https://www.youtube.com/watch?v={video_id}",
        headers={"user-agent": HEADERS["user-agent"], "accept-language": "es-AR,es;q=0.9"})
    html = http(req).decode("utf-8", "replace")
    # El texto visible es confiable; originalViewCount a veces viene en "0".
    m = re.search(r'"videoViewCountRenderer":\{"viewCount":\{"simpleText":"([\d.,]+)', html)
    views = int(re.sub(r"\D", "", m.group(1))) if m else 0
    if not views:
        m = re.search(r'"originalViewCount":"([1-9]\d*)"', html)
        views = int(m.group(1)) if m else 0
    if not views:
        return None, None
    p = re.search(r'(\d{4}-\d\d-\d\d)T\d\d:\d\d:\d\d[+-]\d\d:\d\d', html)
    if p:
        return views, p.group(1)
    t = re.search(r'"dateText":\{"simpleText":"(\d{1,2}) (\w{3})\w*\.? (\d{4})"', html)
    month = t and MONTHS.get(t.group(2).lower())
    published = f"{t.group(3)}-{month:02d}-{int(t.group(1)):02d}" if month else None
    return views, published


def snapshot_views(db):
    """Una foto de visitas por día de todos los videos que alguna vez tuvieron pauta."""
    today = date.today().isoformat()
    ids = [r[0] for r in db.execute(
        "SELECT DISTINCT video_id FROM creatives WHERE video_id IS NOT NULL ORDER BY video_id")]
    ok = 0
    for vid in ids:
        try:
            views, published = fetch_views(vid)
        except urllib.error.URLError as e:
            print(f"  visitas fallo {vid}: {e}", file=sys.stderr)
            continue
        if views is None:
            print(f"  sin visitas para {vid} (¿privado o cambió la página?)", file=sys.stderr)
            continue
        db.execute("INSERT OR REPLACE INTO view_snapshots VALUES (?,?,?,?)",
                   (vid, today, datetime.now().isoformat(timespec="seconds"), views))
        if published:
            db.execute("UPDATE videos SET published=? WHERE video_id=?", (published, vid))
        db.commit()
        ok += 1
        time.sleep(0.7)
    print(f"visitas {today}: {ok}/{len(ids)} videos")


def parse_day(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def run_days(db, advertisers, days):
    for day in days:
        for adv, name in advertisers.items():
            seen, new = track_day(db, adv, day)
            print(f"{day} {name}: {seen} creativos ({new} nuevos)")
    print(f"títulos nuevos: {fill_titles(db)}")
    snapshot_views(db)


def report(db, day):
    rows = db.execute(
        """SELECT c.video_id, COALESCE(v.title, '(sin título)'), c.channel, COUNT(*) AS n
           FROM sightings s JOIN creatives c USING (creative_id) LEFT JOIN videos v USING (video_id)
           WHERE s.day=? AND c.video_id IS NOT NULL
           GROUP BY c.video_id ORDER BY n DESC, v.title""", (day,)).fetchall()
    print(f"Videos con pauta el {day}: {len(rows)}")
    for vid, title, channel, n in rows:
        print(f"  https://youtu.be/{vid}  [{channel}]  x{n}  {title}")


def export_csv(db, out):
    rows = db.execute(
        """SELECT s.day, c.video_id, v.title, c.channel, c.duration, c.creative_id, c.first_shown, c.last_shown
           FROM sightings s JOIN creatives c USING (creative_id) LEFT JOIN videos v USING (video_id)
           ORDER BY s.day, c.video_id""").fetchall()
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["dia", "video_id", "titulo", "canal", "duracion", "creative_id", "primera_vez", "ultima_vez"])
        w.writerows(rows)
    print(f"{len(rows)} filas -> {out}")


def build_ui(db, advertisers, out):
    """Genera un dashboard HTML autocontenido con los datos de la base."""
    rows = db.execute(
        """SELECT s.day, c.video_id, c.duration, COALESCE(v.title, '(sin título)'), COUNT(*)
           FROM sightings s JOIN creatives c USING (creative_id) LEFT JOIN videos v USING (video_id)
           WHERE c.video_id IS NOT NULL GROUP BY s.day, c.video_id ORDER BY s.day""").fetchall()
    all_days = [r[0] for r in db.execute("SELECT DISTINCT day FROM sightings ORDER BY day")]
    if not all_days:
        sys.exit("La base está vacía: corré `run` o `backfill` primero.")
    first, last = parse_day(all_days[0]), parse_day(all_days[-1])
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    index = {d: i for i, d in enumerate(days)}
    per_day, videos = {}, {}
    for day, video_id, duration, title, n in rows:
        per_day.setdefault(day, {})[video_id] = n
        v = videos.setdefault(video_id, {"id": video_id, "title": title, "duration": duration, "days": []})
        v["days"].append(index[day])
    for v in videos.values():
        v["first"], v["last"] = v["days"][0], v["days"][-1]
        v["published"] = None
        v["snaps"] = []
    for vid, published in db.execute("SELECT video_id, published FROM videos"):
        if vid in videos:
            videos[vid]["published"] = published
    for vid, day, views in db.execute(
            """SELECT video_id, day, views FROM view_snapshots
               WHERE (video_id, taken_at) IN (SELECT video_id, MAX(taken_at) FROM view_snapshots GROUP BY video_id, day)
               ORDER BY day"""):
        if vid in videos:
            videos[vid]["snaps"].append([day, views])
    data = {
        "generated": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "advertiser": ", ".join(advertisers.values()),
        "today": date.today().isoformat(),
        "days": days,
        "perDay": per_day,
        "videos": list(videos.values()),
    }
    template = open(Path(__file__).with_name("ui_template.html"), encoding="utf-8").read()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    Path(out).write_text(template.replace("__DATA__", payload), encoding="utf-8")
    print(f"UI -> {out}")


def update(db, advertisers, out):
    """Ciclo completo: anuncios de ayer y hoy, visitas de todos los videos y dashboard."""
    print(f"== {datetime.now():%Y-%m-%d %H:%M:%S} update")
    today = date.today()
    try:
        run_days(db, advertisers, [today - timedelta(days=1), today])
    except (RateLimited, urllib.error.URLError, ValueError) as e:  # si falla Google igual medimos visitas
        print(f"anuncios fallo: {e}", file=sys.stderr)
        snapshot_views(db)
    build_ui(db, advertisers, out)


def main():
    if "--log" in sys.argv:  # para pythonw / tareas programadas: sin consola
        sys.argv.remove("--log")
        sys.stdout = sys.stderr = open(HERE / "update.log", "a", encoding="utf-8", buffering=1)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--advertiser", action="append", metavar="AR…",
                   help="ID de anunciante (repetible). Default: LUZU TV S.A.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--day", type=parse_day, help="default: ayer y hoy")
    sub.add_parser("update", help="run + visitas + dashboard (lo que usa la tarea programada)")
    b = sub.add_parser("backfill")
    b.add_argument("--from", dest="start", type=parse_day, required=True)
    b.add_argument("--to", dest="end", type=parse_day, default=date.today() - timedelta(days=1))
    sub.add_parser("views")
    rp = sub.add_parser("report"); rp.add_argument("--day", type=parse_day)
    c = sub.add_parser("csv"); c.add_argument("--out", default="pauta.csv")
    u = sub.add_parser("ui"); u.add_argument("--out", default=str(HERE / "dashboard.html")); u.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    advertisers = {a: a for a in args.advertiser} if args.advertiser else DEFAULT_ADVERTISERS
    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    migrate(db)
    yesterday = date.today() - timedelta(days=1)

    try:
        dispatch(args, db, advertisers, yesterday)
    except RateLimited as e:
        sys.exit(str(e))


def dispatch(args, db, advertisers, yesterday):
    if args.cmd == "run":
        run_days(db, advertisers, [args.day] if args.day else [yesterday, date.today()])
    elif args.cmd == "update":
        update(db, advertisers, str(HERE / "dashboard.html"))
    elif args.cmd == "views":
        snapshot_views(db)
    elif args.cmd == "backfill":
        n = (args.end - args.start).days + 1
        run_days(db, advertisers, [args.start + timedelta(days=i) for i in range(n)])
    elif args.cmd == "report":
        report(db, (args.day or yesterday).isoformat())
    elif args.cmd == "csv":
        export_csv(db, args.out)
    elif args.cmd == "ui":
        build_ui(db, advertisers, args.out)
        if not args.no_open:
            webbrowser.open(Path(args.out).resolve().as_uri())


if __name__ == "__main__":
    main()
