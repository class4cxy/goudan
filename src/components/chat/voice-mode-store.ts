/**
 * voice-mode-store — 外放模式全局状态
 * ======================================
 * 用轻量的 useSyncExternalStore 模式管理"外放模式"开关，
 * 避免引入 Zustand / Jotai 等额外依赖。
 *
 * 状态持久化到 localStorage（key: "aria:voiceMode"），
 * 刷新页面后保留上次设置。
 */

type Listener = () => void;

const STORAGE_KEY = "aria:voiceMode";

function readStorage(): boolean {
  if (typeof window === "undefined") return false;
  return localStorage.getItem(STORAGE_KEY) === "true";
}

let _voiceMode = readStorage();
const _listeners = new Set<Listener>();

function notify() {
  _listeners.forEach((l) => l());
}

export const voiceModeStore = {
  subscribe(listener: Listener) {
    _listeners.add(listener);
    return () => _listeners.delete(listener);
  },
  getSnapshot(): boolean {
    return _voiceMode;
  },
  getServerSnapshot(): boolean {
    return false; // SSR 默认关闭
  },
  toggle() {
    _voiceMode = !_voiceMode;
    try {
      localStorage.setItem(STORAGE_KEY, String(_voiceMode));
    } catch {
      // ignore
    }
    notify();
  },
  set(value: boolean) {
    if (_voiceMode === value) return;
    _voiceMode = value;
    try {
      localStorage.setItem(STORAGE_KEY, String(_voiceMode));
    } catch {
      // ignore
    }
    notify();
  },
};
