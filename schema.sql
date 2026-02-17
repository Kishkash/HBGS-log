PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bgg_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    is_active INTEGER DEFAULT 1,
    last_full_scan TEXT
);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    name TEXT,
    image_url TEXT
);

CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    play_date TEXT NOT NULL,

    FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES bgg_users(id) ON DELETE SET NULL
);


CREATE INDEX IF NOT EXISTS idx_plays_game_id ON plays(game_id);
CREATE INDEX IF NOT EXISTS idx_plays_play_date ON plays(play_date);
CREATE INDEX IF NOT EXISTS idx_plays_user_id ON plays(user_id);
CREATE INDEX IF NOT EXISTS idx_bgg_users_is_active ON bgg_users(is_active);
