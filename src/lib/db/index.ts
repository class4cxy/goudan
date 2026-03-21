import Database from "better-sqlite3";
import path from "path";
import fs from "fs";

const DB_PATH = path.join(process.cwd(), "data/home-agent.db");
fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });

// Singleton – Next.js hot-reload safe
const globalForDb = globalThis as unknown as { _db: Database.Database | undefined };
const db = globalForDb._db ?? new Database(DB_PATH);
if (process.env.NODE_ENV !== "production") globalForDb._db = db;

db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

db.exec(`
  CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK(role IN ('user','assistant','tool')),
    content     TEXT    NOT NULL,
    tool_name   TEXT,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch())
  );

  CREATE TABLE IF NOT EXISTS threads (
    id         TEXT    PRIMARY KEY,
    title      TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
  );

  CREATE TABLE IF NOT EXISTS thread_messages (
    thread_id  TEXT    NOT NULL PRIMARY KEY,
    messages   TEXT    NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
  );

  CREATE TABLE IF NOT EXISTS cleaning_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rooms       TEXT,
    mode        TEXT,
    triggered_by TEXT,
    duration_s  INTEGER,
    area_m2     REAL,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch())
  );

  CREATE TABLE IF NOT EXISTS inspection_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id    TEXT NOT NULL,
    score        INTEGER,
    has_trash    INTEGER,
    dirty_zones  TEXT,
    action_taken TEXT,
    created_at   INTEGER NOT NULL DEFAULT (unixepoch())
  );

  CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    cron        TEXT NOT NULL,
    task_type   TEXT NOT NULL CHECK(task_type IN ('inspect_and_clean','clean_rooms','clean_full')),
    config      TEXT NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run_at INTEGER,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch())
  );

  CREATE TABLE IF NOT EXISTS preferences (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS conversation_chunks (
    id            TEXT    PRIMARY KEY,
    thread_id     TEXT    NOT NULL,
    level         INTEGER NOT NULL DEFAULT 1,
    message_count INTEGER NOT NULL,
    covers_from   INTEGER NOT NULL,
    covers_to     INTEGER NOT NULL,
    summary       TEXT    NOT NULL,
    token_count   INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
  );
`);

const insertPref = db.prepare(
  "INSERT OR IGNORE INTO preferences (key, value) VALUES (?, ?)"
);
insertPref.run("quiet_hours_start", "22");
insertPref.run("quiet_hours_end", "8");
insertPref.run("clean_mode", "standard");

// ── Types ────────────────────────────────────────────────────────
export interface ScheduledTask {
  id: number;
  name: string;
  cron: string;
  task_type: "inspect_and_clean" | "clean_rooms" | "clean_full";
  config: Record<string, unknown>;
  enabled: boolean;
  last_run_at: number | null;
  created_at: number;
}

export interface RawScheduledTask {
  id: number;
  name: string;
  cron: string;
  task_type: "inspect_and_clean" | "clean_rooms" | "clean_full";
  config: string;
  enabled: number;
  last_run_at: number | null;
  created_at: number;
}

export function parseRawTask(raw: RawScheduledTask): ScheduledTask {
  return {
    ...raw,
    config: JSON.parse(raw.config),
    enabled: raw.enabled === 1,
  };
}

export interface Thread {
  id: string;
  title: string | null;
  created_at: number;
  updated_at: number;
}

export interface ConversationChunk {
  id: string;
  thread_id: string;
  level: number;
  message_count: number;
  covers_from: number;
  covers_to: number;
  summary: string;
  token_count: number;
  created_at: number;
}

export interface InspectionRecord {
  camera_id: string;
  score: number;
  has_trash: boolean;
  dirty_zones: string[];
  action_taken?: string;
}

export interface VoiceLogDay {
  day: string;           // e.g. "2026-03-21"
  session_count: number;
  message_count: number;
  first_ts: number;
  last_ts: number;
}

export interface VoiceLogSession {
  session_id: string;
  started_at: number;
  ended_at: number;
  message_count: number;
}

export interface VoiceLogMessage {
  id: number;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: number;
}

// ── Queries ──────────────────────────────────────────────────────
export const queries = {
  insertConversation: db.prepare(
    "INSERT INTO conversations (session_id, role, content, tool_name) VALUES (?, ?, ?, ?)"
  ),
  getConversations: db.prepare(
    "SELECT * FROM conversations WHERE session_id = ? ORDER BY created_at ASC LIMIT 50"
  ),
  clearConversations: db.prepare("DELETE FROM conversations WHERE session_id = ?"),

  // Voice log queries — returns sessions grouped by calendar day (UTC+8)
  listVoiceLogDays: db.prepare(`
    SELECT
      date(created_at, 'unixepoch', '+8 hours') AS day,
      COUNT(DISTINCT session_id) AS session_count,
      COUNT(*) AS message_count,
      MIN(created_at) AS first_ts,
      MAX(created_at) AS last_ts
    FROM conversations
    WHERE role IN ('user','assistant')
    GROUP BY day
    ORDER BY day DESC
    LIMIT 90
  `),
  listVoiceSessionsByDay: db.prepare(`
    SELECT
      session_id,
      MIN(created_at) AS started_at,
      MAX(created_at) AS ended_at,
      COUNT(*) AS message_count
    FROM conversations
    WHERE role IN ('user','assistant')
      AND date(created_at, 'unixepoch', '+8 hours') = ?
    GROUP BY session_id
    ORDER BY started_at ASC
  `),
  getVoiceSession: db.prepare(`
    SELECT id, session_id, role, content, created_at
    FROM conversations
    WHERE session_id = ? AND role IN ('user','assistant')
    ORDER BY created_at ASC
  `),

  insertCleaningRecord: db.prepare(
    "INSERT INTO cleaning_records (rooms, mode, triggered_by, duration_s, area_m2) VALUES (?, ?, ?, ?, ?)"
  ),
  getCleaningHistory: db.prepare(
    "SELECT * FROM cleaning_records ORDER BY created_at DESC LIMIT ?"
  ),

  insertInspection: db.prepare(
    "INSERT INTO inspection_records (camera_id, score, has_trash, dirty_zones, action_taken) VALUES (?, ?, ?, ?, ?)"
  ),
  getInspections: db.prepare(
    "SELECT * FROM inspection_records ORDER BY created_at DESC LIMIT ?"
  ),

  insertTask: db.prepare(
    "INSERT INTO scheduled_tasks (name, cron, task_type, config) VALUES (?, ?, ?, ?)"
  ),
  getAllTasks: db.prepare("SELECT * FROM scheduled_tasks ORDER BY created_at DESC"),
  updateTaskEnabled: db.prepare("UPDATE scheduled_tasks SET enabled = ? WHERE id = ?"),
  updateTaskLastRun: db.prepare(
    "UPDATE scheduled_tasks SET last_run_at = unixepoch() WHERE id = ?"
  ),
  deleteTask: db.prepare("DELETE FROM scheduled_tasks WHERE id = ?"),

  getPref: db.prepare("SELECT value FROM preferences WHERE key = ?"),
  setPref: db.prepare("INSERT OR REPLACE INTO preferences (key, value) VALUES (?, ?)"),

  // Thread management
  createThread: db.prepare(
    "INSERT OR IGNORE INTO threads (id) VALUES (?)"
  ),
  getThread: db.prepare("SELECT * FROM threads WHERE id = ?"),
  listThreads: db.prepare(
    "SELECT * FROM threads ORDER BY updated_at DESC LIMIT 100"
  ),
  setThreadTitle: db.prepare(
    "UPDATE threads SET title = ?, updated_at = unixepoch() WHERE id = ?"
  ),
  touchThread: db.prepare(
    "UPDATE threads SET updated_at = unixepoch() WHERE id = ?"
  ),
  deleteThread: db.prepare("DELETE FROM threads WHERE id = ?"),

  // Thread messages (full UIMessage[] stored as JSON)
  saveThreadMessages: db.prepare(
    `INSERT INTO thread_messages (thread_id, messages, updated_at)
     VALUES (?, ?, unixepoch())
     ON CONFLICT(thread_id) DO UPDATE SET messages = excluded.messages, updated_at = unixepoch()`
  ),
  getThreadMessages: db.prepare(
    "SELECT messages FROM thread_messages WHERE thread_id = ?"
  ),

  // Conversation chunks (compressed history for ConversationBuffer)
  insertChunk: db.prepare(
    `INSERT INTO conversation_chunks
       (id, thread_id, level, message_count, covers_from, covers_to, summary, token_count)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  ),
  getThreadChunks: db.prepare(
    "SELECT * FROM conversation_chunks WHERE thread_id = ? ORDER BY covers_from ASC"
  ),
  getChunksByLevel: db.prepare(
    "SELECT * FROM conversation_chunks WHERE thread_id = ? AND level = ? ORDER BY covers_from ASC"
  ),
  deleteChunk: db.prepare("DELETE FROM conversation_chunks WHERE id = ?"),
  sumChunkMessages: db.prepare(
    "SELECT COALESCE(SUM(message_count), 0) AS total FROM conversation_chunks WHERE thread_id = ?"
  ),
};

export function getPreference(key: string): string | null {
  const row = queries.getPref.get(key) as { value: string } | undefined;
  return row?.value ?? null;
}

export function isQuietHour(): boolean {
  const start = parseInt(getPreference("quiet_hours_start") ?? "22");
  const end = parseInt(getPreference("quiet_hours_end") ?? "8");
  const hour = new Date().getHours();
  if (start > end) return hour >= start || hour < end;
  return hour >= start && hour < end;
}

export default db;
