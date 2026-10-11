-- User labels for Wi-Fi and BLE devices (docs/history.md "Labels"). Kept in
-- the main database so backup and restore carry them; the bulk history lives
-- in history.db.
CREATE TABLE device_labels (
    receiver TEXT NOT NULL CHECK (receiver IN ('wifi', 'ble')),
    ident TEXT NOT NULL,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    class TEXT NOT NULL CHECK (class IN ('mine', 'known', 'ignore')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (receiver, ident)
);
