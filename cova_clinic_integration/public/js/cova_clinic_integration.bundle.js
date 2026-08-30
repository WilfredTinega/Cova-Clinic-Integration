// Desk bundle for the app.
//
// This exists so the asset is served under a content-hashed filename. A plain
// "/assets/.../form_walkthrough.js" entry in app_include_js is emitted verbatim
// with no version query, so browsers keep serving a cached copy after a deploy
// and fixes appear not to have landed. Anything bundled through here is
// resolved via assets.json and its name changes whenever the contents do.

import "./form_walkthrough";
