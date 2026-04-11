import os
import time
import sqlite3
import requests
from flask import session, redirect, url_for
from functools import wraps
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, abort, render_template
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH")
BGG_TOKEN = os.environ.get("BGG_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")
# Sets the location the app is looking for in bgg logged plays
GAME_LOCATION = os.environ.get("GAME_LOCATION")
# Variables setting the limits for scanning plays
START_YEAR = os.environ.get("START_YEAR")
START_DATE = f"{START_YEAR}-01-01"
RESCAN_DAYS = 14
# Max games displayed on site
GAME_LIMIT = 200
# Variable for locking admin login
ALLOWED_ATTEMPTS = 5
LOCK_TIME = 5
FAILED_ATTEMPTS = {
    "count": 0,
    "locked_until": None
}

app = Flask(__name__)

app.secret_key = SECRET_KEY


def get_db():
    """Opens a connection to the database file in g(flask storage) and returns it"""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        # makes rows behave as tuples AND dictionaries
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON;")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    """On teardown closes the db connection"""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """On first run creates the database file from the schema"""
    with app.app_context():
        db = get_db()
        with open("schema.sql", "r") as f:
            db.executescript(f.read())
        db.commit()


# ---------- Admin: manage BGG users ----------

def require_admin(func):
    """Allows only the admin to use a function"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return func(*args, **kwargs)
    return wrapper


@app.route("/admin")
@require_admin
def admin_dashboard():
    """Renders the admin page"""
    db = get_db()
    users = db.execute("SELECT id, username, last_full_scan FROM bgg_users ORDER BY username").fetchall()

    return render_template("admin_dashboard.html", users=users)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    now = datetime.now()

    # Check if account is locked
    if FAILED_ATTEMPTS["locked_until"] and now < FAILED_ATTEMPTS["locked_until"]:
        remaining = FAILED_ATTEMPTS["locked_until"] - now
        minutes = int(remaining.total_seconds() // 60) + 1
        return render_template(
            "admin_login.html",
            error=f"Account locked. Try again in {minutes} minutes."
        )
    # If locked, after "locked_until" has passed, reset
    elif FAILED_ATTEMPTS["locked_until"]:
        FAILED_ATTEMPTS["count"] = 0
        FAILED_ATTEMPTS["locked_until"] = None

    if request.method == "POST":
        password = request.form.get("password")

        if password == ADMIN_PASSWORD:
            # Reset failed attempts
            FAILED_ATTEMPTS["count"] = 0

            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))

        # Failed password
        FAILED_ATTEMPTS["count"] += 1

        if FAILED_ATTEMPTS["count"] >= ALLOWED_ATTEMPTS:
            FAILED_ATTEMPTS["locked_until"] = now + timedelta(minutes=LOCK_TIME)
            return render_template(
                "admin_login.html",
                error=F"Too many failed attempts. Account locked for {LOCK_TIME} minutes."
            )

        return render_template(
            "admin_login.html",
            error="Invalid password"
        )

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/api/admin/users", methods=["POST"])
@require_admin
def add_user():
    """Adds a bgg username to the database"""
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
        return jsonify({"status": "user exists"})

    # Create new user
    db.execute(
        "INSERT INTO bgg_users (username, last_full_scan) VALUES (?, Null)",
        (username,)
    )
    db.commit()
    return jsonify({"status": "created"})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@require_admin
def delete_user(user_id):
    """Deletes a user from the database, deletes all the user's plays as well due to foreign key cascade."""
    db = get_db()
    db.execute(
        "DELETE FROM bgg_users WHERE id=?",
        (user_id,)
    )
    db.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/admin/fullscan/<username>", methods=["POST"])
@require_admin
def admin_full_scan(username):
    """Performs a full scan of a user's plays from the START_DATE onwards. Deletes all the user's existing plays
    from the database and logs everything again."""
    db = get_db()

    # Get user id
    user = db.execute(
        "SELECT id FROM bgg_users WHERE username=?",
        (username,)
    ).fetchone()

    if not user:
        return jsonify({"status": "full scan failed, user doesn't exist", "user": username})

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

    return jsonify({"status": "full scan complete", "user": username})


@app.route("/api/admin/run_cron", methods=["POST"])
@require_admin
def api_run_cron():
    """Manually updates plays for all users for the last RESCAN_DAYS"""
    # Call existing cron logic
    response = cron_update()

    return jsonify({"status": "cron executed", "details": response})


# ---------- BGG fetching & cron endpoint ----------
def fetch_plays_for_user(username: str, user_id: int, full_scan: bool):
    """Helper function, used in cron_update and admin_full_scan. Calls the bgg api, retrieving play elements
    from each page of the user's logged plays, until reaching the cutoff date."""

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
            return new_plays

        for p in play_elems:
            play_id = int(p.attrib.get("id"))
            play_date = p.attrib.get("date")

            # Stop if the play is older than the re-scan window
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
    """Helper function that actually logs the plays - used in cron_update and admin_full_scan.
    It first checks the game exists in the games table, to make sure game_id exists so the log doesn't fail
    because of the foreign key. If the game doesn't exist calls the fetch_game_info helper function."""

    db = get_db()
    # Save the id of every play to later check for deleted plays
    bgg_ids = set()

    for p in plays:
        bgg_ids.add(p["id"])

        # Insert play if not already present
        exists = db.execute(
            "SELECT 1 FROM plays WHERE id=?",
            (p["id"],)
        ).fetchone()

        # Remove play if location changed
        if exists and p["location"] != GAME_LOCATION:
            db.execute(
                "DELETE FROM plays WHERE id=?",
                (p["id"],)
            )
            continue
        elif not exists and p["location"] == GAME_LOCATION:
            # Ensure game exists in games table
            game = db.execute(
                "SELECT id FROM games WHERE id=?",
                (p["game_id"],)
            ).fetchone()

            if game is None:
                info = fetch_game_info(p["game_id"])
                db.execute(
                    "INSERT INTO games (id, name, image_url) VALUES (?, ?, ?)",
                    (p["game_id"], info["name"], info["image_url"])
                )

            # Insert play data to db
            db.execute(
                "INSERT INTO plays (id, game_id, play_date, user_id) VALUES (?, ?, ?, ?)",
                (p["id"], p["game_id"], p["play_date"], p["user_id"])
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

    # Detect deleted plays
    if plays:
        user_id = plays[0]["user_id"]

        # Set the time period to check
        if full_scan:
            cutoff = START_DATE
        else:
            cutoff = (datetime.today() - timedelta(days=RESCAN_DAYS)).isoformat()

        # Get user's play ids for the chosen time period
        db_ids = {
            row["id"]
            for row in db.execute(
                "SELECT id FROM plays WHERE user_id=? AND play_date >= ?",
                (user_id, cutoff)
            ).fetchall()
        }

        # Compare user's current play ids with the ones in the db to find deleted ids
        deleted_ids = db_ids - bgg_ids

        for play_id in deleted_ids:
            db.execute("DELETE FROM plays WHERE id=?", (play_id,))

        # Update last_full_scan if full scan
        if full_scan:
            today = datetime.today().isoformat(timespec="seconds")
            db.execute(
                "UPDATE bgg_users SET last_full_scan=? WHERE id=?",
                (today, user_id)
            )

    db.commit()


def fetch_game_info(game_id: int):
    """Helper function that calls the bgg api to get a game's info by its id, returns the game's name and box image.
    Used in update_plays."""

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

    return {
        "name": name,
        "image_url": image_url
    }


def cron_update():
    """Updates the plays in the database for all the users. If user was never scanned - performs full scan
     otherwise timeframe is RESCAN_DAYS. Used manually by admin and from outside the app by cron jobs."""

    db = get_db()
    users = db.execute("SELECT username, id, last_full_scan FROM bgg_users").fetchall()

    if not users:
        return {"status": "No active users"}

    # Fetch and update plays for each user
    for row in users:
        username = row["username"]
        user_id = row["id"]
        # If user was never scanned perform full scan
        full_scan = True if (row["last_full_scan"] is None) else False
        plays = fetch_plays_for_user(username, user_id, full_scan=full_scan)
        update_plays(plays, full_scan)

    return {"status": "ok"}


# ---------- Top Bar Routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/game-stats")
def game_stats():
    return render_template("stats.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


# ---------- Stats API ----------
@app.route("/api/stats", methods=["GET"])
def stats():
    """Gets the required plays from the db to display on the website."""
    period = request.args.get("period", "overall")  # overall, year, month, date
    year = request.args.get("year")
    month = request.args.get("month")
    date = request.args.get("date")

    db = get_db()
    where = []
    params = []
    limit = GAME_LIMIT

    if period == "year" and year:
        where.append("substr(plays.play_date,1,4) = ?")
        params.append(year)
    elif period == "month" and year and month:
        where.append("substr(plays.play_date,1,7) = ?")
        params.append(f"{year}-{month.zfill(2)}")
    elif period == "date" and date:
        where.append("plays.play_date = ?")
        params.append(date)
    elif period == "recent":
        limit = 10

    params.append(limit)

    where_clause = "WHERE " + " AND ".join(where) if where else ""

    sql = f"""
        SELECT 
            plays.game_id,
            games.name,
            games.image_url,
            COUNT(*) AS plays,
            MAX(plays.play_date) AS last_play_date
        FROM plays
        JOIN games ON plays.game_id = games.id
        {where_clause}
        GROUP BY plays.game_id, games.name, games.image_url
        ORDER BY plays DESC, last_play_date DESC
        LIMIT (?)
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
    app.run(debug=False)
