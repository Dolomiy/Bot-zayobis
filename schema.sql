-- Схема бази даних для бота контролю повернень

CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date             TEXT    NOT NULL,          -- YYYY-MM-DD
    deadline_time    TEXT    NOT NULL,          -- HH:MM
    status           TEXT    NOT NULL DEFAULT 'pending',  -- pending / done / done_late / missed / skipped
    photo_file_id    TEXT,
    confirmed_by_user_id INTEGER,
    confirmed_at     TEXT,                      -- ISO datetime
    notified_pre     INTEGER NOT NULL DEFAULT 0,   -- 1 якщо перше нагадування надіслано
    notified_final   INTEGER NOT NULL DEFAULT 0,   -- 1 якщо друге нагадування надіслано
    skip_reason      TEXT,
    UNIQUE(date, deadline_time)
);
