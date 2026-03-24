/**
 * camera-stream-store — 摄像头直播流全局状态
 * =============================================
 * 与 voice-mode-store 相同的轻量 useSyncExternalStore 模式。
 * 不做持久化：刷新页面后默认关闭直播流（摄像头连接是会话级的）。
 */

type Listener = () => void;

let _active = false;
const _listeners = new Set<Listener>();

function notify() {
  _listeners.forEach((l) => l());
}

export const cameraStreamStore = {
  subscribe(listener: Listener) {
    _listeners.add(listener);
    return () => _listeners.delete(listener);
  },
  getSnapshot(): boolean {
    return _active;
  },
  getServerSnapshot(): boolean {
    return false;
  },
  open() {
    if (_active) return;
    _active = true;
    notify();
  },
  close() {
    if (!_active) return;
    _active = false;
    notify();
  },
};
