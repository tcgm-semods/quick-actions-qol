# Quick Actions QoL

Two quality-of-life changes to the bottom-center Quick Actions (hotbar)
panel:

1. **Draggable** -- click and drag the "QUICK ACTIONS" header to move the
   panel anywhere on screen. Position is kept in memory for the session
   (resets to the default centered-bottom position on restart).
2. **Fighter auto-launch** -- while "in combat" (recently took a hit, or
   currently have a hostile target selected), any *locked* hotbar slot
   bound to a fighter or a ready fighter-wing fabricator is activated
   automatically every ~0.25 seconds (about as fast as an attentive player
   could sustain manually mashing the hotkey -- not superhuman). Each
   fighter/fabricator slot gets its
   own small lightning-bolt lock badge in its corner -- click a badge to
   lock that slot on or off independently. Only the first such slot ever
   seen starts locked on; every other one starts off. A small
   "FIGHTERS: n" (or "n/cap" once a cap is discovered) readout appears in
   the panel's left margin whenever any such slot exists, so you can
   check the tracked count against what's actually happening in game.

   The game never tells the client how many fighters are currently
   deployed, so this mod tracks it itself, counting *every* launch --
   whether triggered by a manual key press, a manual click, or this
   mod's own auto-launch, since all three go through the same
   underlying activation function this mod hooks. It counts up by 1 for
   every launch (a wing counts as one slot, same as a single fighter),
   counts back down when the log reports a wing "retired" (returned on
   its own) or "destroyed" (wiped out in combat) -- either way frees the
   slot -- and if the log ever says a launch "cannot" proceed, it treats
   that as discovering the real capacity and stops trying past it,
   resuming automatically the moment a "retired"/"destroyed" line drops
   the count back down. No fixed wait, no guessing.

   These three keywords were confirmed against a real dungeon run
   recorded by the separate `chat-log-recorder` mod -- that's also how
   "destroyed" was found: the real line is "Liberty Dart destroyed."
   (the fighter's display name, not the word "fighter"), which a
   fighter-only keyword filter would have missed entirely. Keep
   `chat-log-recorder` installed and check its logs after future
   sessions if auto-launch ever seems to drift out of sync.
