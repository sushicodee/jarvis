// Floating Jarvis HUD: transparent, always-on-top, all Spaces, click-through except the orb/bubble.
const { app, BrowserWindow, ipcMain, Menu, screen } = require("electron");
const fs = require("fs");
const os = require("os");
const path = require("path");

const API = "http://127.0.0.1:8765";
const STATE_FILE = path.join(os.homedir(), ".cache", "jarvis", "hud.json");
const W = 420;
const H = 170;

let win;
let dragOffset = null;

function loadPos() {
  try {
    const p = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
    const visible = screen.getAllDisplays().some((d) => {
      const b = d.workArea;
      return p.x >= b.x - 50 && p.x < b.x + b.width && p.y >= b.y - 50 && p.y < b.y + b.height;
    });
    if (visible) return p;
  } catch {}
  const wa = screen.getPrimaryDisplay().workArea;
  return { x: wa.x + wa.width - W - 20, y: wa.y + wa.height - H - 20 };
}

function savePos() {
  if (!win) return;
  const [x, y] = win.getPosition();
  fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify({ x, y }));
}

async function post(p) {
  try {
    await fetch(API + p, { method: "POST" });
  } catch {}
}

app.whenReady().then(() => {
  if (app.dock) app.dock.hide();
  const pos = loadPos();
  win = new BrowserWindow({
    x: pos.x, y: pos.y, width: W, height: H,
    transparent: true, frame: false, resizable: false, hasShadow: false,
    alwaysOnTop: true, skipTaskbar: true, focusable: false, fullscreenable: false,
    webPreferences: { preload: path.join(__dirname, "preload.js") },
  });
  win.setAlwaysOnTop(true, "floating");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(true, { forward: true });
  win.loadFile("index.html");

  ipcMain.on("mouse", (_e, interactive) => {
    win.setIgnoreMouseEvents(!interactive, { forward: true });
  });
  ipcMain.on("drag-start", (_e, { sx, sy }) => {
    const [x, y] = win.getPosition();
    dragOffset = { dx: sx - x, dy: sy - y };
  });
  ipcMain.on("drag-move", (_e, { sx, sy }) => {
    if (dragOffset) win.setPosition(Math.round(sx - dragOffset.dx), Math.round(sy - dragOffset.dy));
  });
  ipcMain.on("drag-end", () => {
    dragOffset = null;
    savePos();
  });
  ipcMain.on("toggle", () => post("/toggle"));
  ipcMain.on("menu", () => {
    Menu.buildFromTemplate([
      { label: "Talk / stop  (⌃⌥J)", click: () => post("/toggle") },
      { label: "Cancel / shut up  (⌃⌥K)", click: () => post("/cancel") },
      { type: "separator" },
      { label: "Reset position", click: () => {
          const wa = screen.getPrimaryDisplay().workArea;
          win.setPosition(wa.x + wa.width - W - 20, wa.y + wa.height - H - 20);
          savePos();
        } },
      { label: "Quit HUD", click: () => app.quit() },
    ]).popup({ window: win });
  });
});

app.on("window-all-closed", () => app.quit());
