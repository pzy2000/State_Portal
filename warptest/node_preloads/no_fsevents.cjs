const Module = require("module");

const originalLoad = Module._load;

Module._load = function warptestLoad(request, parent, isMain) {
  if (request === "fsevents") {
    const error = new Error("WarpTest disabled fsevents native watcher for benchmark stability.");
    error.code = "MODULE_NOT_FOUND";
    throw error;
  }
  return originalLoad.apply(this, arguments);
};
