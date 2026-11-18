const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("hud", {
  mouse: (interactive) => ipcRenderer.send("mouse", interactive),
  dragStart: (sx, sy) => ipcRenderer.send("drag-start", { sx, sy }),
  dragMove: (sx, sy) => ipcRenderer.send("drag-move", { sx, sy }),
  dragEnd: () => ipcRenderer.send("drag-end"),
  toggle: () => ipcRenderer.send("toggle"),
  menu: () => ipcRenderer.send("menu"),
});
