-- اسکیمای جدول‌های پنل PasarGuard که بات از آن‌ها می‌خواند.
-- نام ستون‌ها عیناً از app/db/models.py در ریپازیتوری PasarGuard/panel گرفته شده.

CREATE TABLE admins (
    id INTEGER PRIMARY KEY,
    username VARCHAR(34) UNIQUE,
    hashed_password VARCHAR(128),
    used_traffic BIGINT DEFAULT 0,
    data_limit BIGINT,
    status VARCHAR(9) DEFAULT 'active',
    last_status_change DATETIME,
    role_id BIGINT DEFAULT 0,
    created_at DATETIME
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username VARCHAR(128) UNIQUE,
    status VARCHAR(9) DEFAULT 'active',
    used_traffic BIGINT DEFAULT 0,
    data_limit BIGINT,
    data_limit_reset_strategy VARCHAR(7) DEFAULT 'no_reset',
    expire DATETIME,
    admin_id BIGINT REFERENCES admins(id),
    sub_revoked_at DATETIME,
    note VARCHAR(500),
    online_at DATETIME,
    on_hold_expire_duration BIGINT,
    on_hold_timeout DATETIME,
    auto_delete_in_days INTEGER,
    hwid_limit BIGINT,
    edit_at DATETIME,
    last_status_change DATETIME,
    created_at DATETIME
);

CREATE TABLE user_usage_logs (
    id INTEGER PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    used_traffic_at_reset BIGINT NOT NULL,
    reset_at DATETIME
);
CREATE INDEX ix_user_usage_logs_user_id_reset_at ON user_usage_logs(user_id, reset_at);

CREATE TABLE admin_usage_logs (
    id INTEGER PRIMARY KEY,
    admin_id BIGINT REFERENCES admins(id),
    used_traffic_at_reset BIGINT NOT NULL,
    reset_at DATETIME
);

CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
    name VARCHAR(256),
    status VARCHAR(9),
    created_at DATETIME
);

CREATE TABLE node_user_usages (
    id INTEGER PRIMARY KEY,
    created_at DATETIME,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    node_id BIGINT REFERENCES nodes(id) ON DELETE CASCADE,
    used_traffic BIGINT DEFAULT 0,
    UNIQUE(created_at, user_id, node_id)
);
CREATE INDEX ix_node_user_usages_user_id_created_at ON node_user_usages(user_id, created_at);
