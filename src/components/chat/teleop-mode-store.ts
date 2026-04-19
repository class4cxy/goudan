"use client";

type Listener = () => void;

let _enabled = false;
const _listeners = new Set<Listener>();

function notify() {
  _listeners.forEach((listener) => listener());
}

export const teleopModeStore = {
  subscribe(listener: Listener) {
    _listeners.add(listener);
    return () => _listeners.delete(listener);
  },
  getSnapshot(): boolean {
    return _enabled;
  },
  getServerSnapshot(): boolean {
    return false;
  },
  enable() {
    if (_enabled) return;
    _enabled = true;
    notify();
  },
  disable() {
    if (!_enabled) return;
    _enabled = false;
    notify();
  },
};
