/**
 * Shared by the server-rendered <head> and the client theme store, so it lives
 * in a module of its own (no "use client").
 */
export const THEME_STORAGE_KEY = "jf-theme";
export const DARK_QUERY = "(prefers-color-scheme: dark)";

/** Runs while the HTML is parsed, so the page never flashes the wrong theme. */
export const THEME_SCRIPT = `(function(){try{var t=localStorage.getItem("${THEME_STORAGE_KEY}");var d=t==="dark"||(t!=="light"&&window.matchMedia("${DARK_QUERY}").matches);document.documentElement.classList.toggle("dark",d)}catch(e){}})()`;
