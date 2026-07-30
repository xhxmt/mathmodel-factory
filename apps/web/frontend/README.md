# Frontend source boundary

The Vue application is currently kept at `web/frontend/` so existing deploy
automation and public paths remain compatible. `package-lock.json` is the
authoritative dependency lock. Install with `npm ci`; runtime launchers do not
install packages.
