import os
import time
import sqlite3
import requests
from flask import session, redirect, url_for
from functools import wraps
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, abort, send_from_directory, render_template
from dotenv import load_dotenv

load_dotenv()

DB_PATH = "bgg_group.db"
BGG_TOKEN = os.environ.get("BGG_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
GAME_LOCATION = os.environ.get("GAME_LOCATION")
START_YEAR = os.environ.get("START_YEAR")
START_DATE = f"{START_YEAR}-01-01"
RESCAN_DAYS = 14

app = Flask(__name__)

app.secret_key = SECRET_KEY


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        with open("schema.sql", "r") as f:
            db.executescript(f.read())
        db.commit()


def require_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            abort(401)
        return func(*args, **kwargs)
    return wrapper


def require_admin_page(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return func(*args, **kwargs)
    return wrapper


# ---------- Admin: manage BGG users ----------

@app.route("/admin")
@require_admin_page
def admin_dashboard():

    db = get_db()
    users = db.execute("SELECT id, username, is_active, last_full_scan FROM bgg_users ORDER BY username").fetchall()

    return render_template("admin_dashboard.html", users=users)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_login.html", error="Invalid password")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/api/admin/users", methods=["GET"])
@require_admin
def list_users():
    db = get_db()
    rows = db.execute(
        "SELECT id, username, is_active, last_full_scan FROM bgg_users ORDER BY username"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/users", methods=["POST"])
@require_admin
def add_user():
    data = request.get_json(force=True)
    username = data.get("username")
    if not username:
        abort(400, "username required")

    db = get_db()

    # Check if user exists
    existing = db.execute(
        "SELECT id FROM bgg_users WHERE username=?",
        (username,)
    ).fetchone()

    if existing:
        # Reactivate user
        db.execute(
            "UPDATE bgg_users SET is_active=1 WHERE username=?",
            (username,)
        )
        db.commit()
        return jsonify({"status": "reactivated"})

    # Create new user
    db.execute(
        "INSERT INTO bgg_users (username, is_active, last_full_scan) VALUES (?, 1, Null)",
        (username,)
    )
    db.commit()
    return jsonify({"status": "created"})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@require_admin
def delete_user(user_id):
    db = get_db()
    db.execute(
        "UPDATE bgg_users SET is_active=0 WHERE id=?",
        (user_id,)
    )
    db.commit()
    return jsonify({"status": "deactivated"})


@app.route("/api/admin/fullscan/<username>", methods=["POST"])
@require_admin
def admin_full_scan(username):
    db = get_db()

    # Get user id
    user = db.execute(
        "SELECT id FROM bgg_users WHERE username=? AND is_active=1",
        (username,)
    ).fetchone()

    if not user:
        return []  # user inactive or missing

    user_id = user["id"]

    # Fetch ALL plays for this user
    plays = fetch_plays_for_user(username, user_id, full_scan=True)

    # Clear existing plays for this user
    db.execute(
        "DELETE FROM plays WHERE user_id=?",
        (user_id,)
    )

    # Re-insert plays
    update_plays(plays, full_scan=True)

    today = datetime.today().isoformat(timespec="seconds")

    db.execute(
        "UPDATE bgg_users SET last_full_scan=? WHERE id=?",
        (today, user_id)
    )
    db.commit()

    return jsonify({"status": "full scan complete", "user": username})


@app.route("/api/admin/fix_missing_images", methods=["POST"])
@require_admin
def api_fix_missing_images():
    db = get_db()

    rows = db.execute(
        "SELECT id FROM games WHERE image_url IS NULL OR image_url = ''"
    ).fetchall()

    fixed = 0

    for row in rows:
        game_id = row["id"]
        info = fetch_game_info(game_id)

        if info["image_url"]:
            db.execute(
                "UPDATE games SET image_url=? WHERE id=?",
                (info["image_url"], game_id)
            )
            fixed += 1

    db.commit()

    return jsonify({"status": "ok", "fixed": fixed})


@app.route("/api/admin/run_cron", methods=["POST"])
@require_admin
def api_run_cron():
    # Call existing cron logic
    response = cron_update()

    return jsonify({"status": "cron executed", "details": response})


# ---------- BGG fetching & cron endpoint ----------
def fetch_plays_for_user(username: str, user_id: int, full_scan: bool):
    # Re-scan window (days)
    if full_scan:
        cutoff_date = START_DATE
    else:
        cutoff_date = (datetime.today() - timedelta(days=RESCAN_DAYS)).isoformat()

    # Get plays from bgg
    page = 1
    new_plays = []
    headers = {
        "Authorization": f"Bearer {BGG_TOKEN}"
    }

    while True:
        url = f"https://boardgamegeek.com/xmlapi2/plays?username={username}&page={page}"
        r = requests.get(url, headers=headers, timeout=20)

        if r.status_code == 202:
            print("BGG says: data not ready yet (202). Retrying...")
            time.sleep(2)
            continue  # try again

        if r.status_code != 200:
            print("BAD STATUS:", r.status_code)
            print("BODY:", r.text[:500])
            break

        root = ET.fromstring(r.text)
        play_elems = root.findall("play")

        if not play_elems:
            break

        # If the newest play on this page is older than the cutoff, stop fetching more pages
        page_newest_date = play_elems[0].attrib.get("date")

        if page_newest_date < START_DATE:
            print("Reached cutoff year, stopping API calls")
            return new_plays

        for p in play_elems:
            play_id = int(p.attrib.get("id"))
            play_date = p.attrib.get("date")

            # Stop if the play is older than our re-scan window
            if play_date < cutoff_date:
                print("Reached cutoff date:", play_date)
                return new_plays

            # For filtering by location
            location = p.attrib.get("location").upper()

            item = p.find("item")
            if item is None:
                continue

            game_id = int(item.attrib.get("objectid"))

            new_plays.append({
                "id": play_id,
                "game_id": game_id,
                "play_date": play_date,
                "user_id": user_id,
                "location": location
            })

        page += 1

    return new_plays


def update_plays(plays, full_scan: bool):
    """Helper function that actually logs the plays - used in cron_update and admin_full_scan"""
    db = get_db()

    for p in plays:
        # Insert play if not already present
        exists = db.execute(
            "SELECT 1 FROM plays WHERE id=?",
            (p["id"],)
        ).fetchone()

        # Remove if location changed
        if exists and p["location"] != GAME_LOCATION:
            db.execute(
                "DELETE FROM plays WHERE id=?",
                (p["id"],)
            )
            continue
        elif not exists and p["location"] == GAME_LOCATION:
            db.execute(
                "INSERT INTO plays (id, game_id, play_date, user_id) VALUES (?, ?, ?, ?)",
                (p["id"], p["game_id"], p["play_date"], p["user_id"])
            )

            # Ensure game exists in games table
            game = db.execute(
                "SELECT id FROM games WHERE id=?",
                (p["game_id"],)
            ).fetchone()

            if game is None:
                print("Fetching game info from BGG:", p["game_id"])
                info = fetch_game_info(p["game_id"])
                db.execute(
                    "INSERT INTO games (id, name, image_url) VALUES (?, ?, ?)",
                    (p["game_id"], info["name"], info["image_url"])
                )
        else:
            # Update the old entry if anything changed
            db.execute(
                """
                UPDATE plays
                SET game_id = ?, play_date = ?, user_id = ?
                WHERE id = ?
                  AND (game_id != ? OR play_date != ? OR user_id != ?)
                """,
                (
                    p["game_id"], p["play_date"], p["user_id"], p["id"],
                    p["game_id"], p["play_date"], p["user_id"]
                )
            )

    # --- NEW: detect deleted plays ---
    if plays:
        user_id = plays[0]["user_id"]

        bgg_ids = {p["id"] for p in plays}

        # set ids to check depending on full or partial scan (date window)
        if full_scan:
            cutoff = START_DATE
        else:
            cutoff = (datetime.today() - timedelta(days=RESCAN_DAYS)).isoformat()

        db_ids = {
            row["id"]
            for row in db.execute(
                "SELECT id FROM plays WHERE user_id=? AND play_date >= ?",
                (user_id, cutoff)
            ).fetchall()
        }

        deleted_ids = db_ids - bgg_ids

        for play_id in deleted_ids:
            db.execute("DELETE FROM plays WHERE id=?", (play_id,))

    db.commit()


def fetch_game_info(game_id: int):
    headers = {
        "Authorization": f"Bearer {BGG_TOKEN}"
    }
    url = f"https://boardgamegeek.com/xmlapi2/thing?id={game_id}"
    while True:
        r = requests.get(url, headers=headers, timeout=20)
        print("GAME STATUS:", r.status_code)

        # BGG queues some requests
        if r.status_code == 202:
            print("BGG says data not ready (202). Retrying...")
            time.sleep(2)
            continue

        if r.status_code != 200:
            print("GAME FETCH FAILED:", r.status_code)
            print("BODY:", r.text[:500])
            return {"name": None, "image_url": None}

        break

    root = ET.fromstring(r.text)
    item = root.find("item")
    if item is None:
        print("NO ITEM FOUND IN GAME INFO")
        return {"name": None, "image_url": None}

    # Game name
    name_elem = item.find("name")
    name = name_elem.attrib.get("value") if name_elem is not None else None

    # Game image
    image_elem = item.find("image")
    image_url = image_elem.text if image_elem is not None else None

    print("GAME INFO FETCHED:", name, image_url)

    return {
        "name": name,
        "image_url": image_url
    }


def cron_update():
    print("CRON UPDATE CALLED")

    db = get_db()
    users = db.execute("SELECT username, id, last_full_scan FROM bgg_users WHERE is_active=1").fetchall()

    if not users:
        return {"status": "No active users"}

    # Fetch and update plays for each user
    for row in users:
        username = row["username"]
        user_id = row["id"]
        full_scan = True if (row["last_full_scan"] is None) else False
        plays = fetch_plays_for_user(username, user_id, full_scan=full_scan)
        update_plays(plays, full_scan)

        if row["last_full_scan"] is None:
            today = datetime.today().isoformat(timespec="seconds")
            db.execute(
                "UPDATE bgg_users SET last_full_scan=? WHERE id=?",
                (today, user_id)
            )
            db.commit()

    return {"status": "ok"}


# ---------- INDEX ----------

@app.route("/")
def index():
    return render_template("index.html")


# ---------- Stats API ----------
@app.route("/api/stats", methods=["GET"])
def stats():
    period = request.args.get("period", "overall")  # overall, year, month, date
    year = request.args.get("year")
    month = request.args.get("month")
    date = request.args.get("date")

    db = get_db()
    where = []
    params = []

    if period == "year" and year:
        where.append("substr(plays.play_date,1,4) = ?")
        params.append(year)
    elif period == "month" and year and month:
        where.append("substr(plays.play_date,1,7) = ?")
        params.append(f"{year}-{month.zfill(2)}")
    elif period == "date" and date:
        where.append("plays.play_date = ?")
        params.append(date)

    where_clause = "WHERE " + " AND ".join(where) if where else ""

    sql = f"""
        SELECT 
            plays.game_id,
            games.name,
            games.image_url,
            COUNT(*) AS plays
        FROM plays
        JOIN games ON plays.game_id = games.id
        {where_clause}
        GROUP BY plays.game_id, games.name, games.image_url
        ORDER BY plays DESC
    """

    rows = db.execute(sql, params).fetchall()

    result = []
    for idx, r in enumerate(rows, start=1):
        result.append({
            "index": idx,
            "game_id": r["game_id"],
            "game_name": r["name"],
            "image_url": r["image_url"],
            "plays": r["plays"]
        })

    return jsonify(result)


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        init_db()
    app.run(debug=True)
