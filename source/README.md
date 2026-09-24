# Quick Actions QoL

Two quality-of-life changes to the Quick Actions (hotbar) panel:

1. **Draggable** -- drag the "QUICK ACTIONS" header anywhere on screen.
   Resets to centered-bottom on restart.
2. **Fighter auto-launch** -- while in combat, locked fighter/fabricator
   hotbar slots fire automatically every ~0.25s (about as fast as an
   attentive player mashing the key -- not superhuman). Each slot gets a
   lightning-bolt badge to lock it on or off, and a "FIGHTERS: n" readout
   tracks how many are out, since the game itself never tells the client
   that number.

Fighter counts are inferred from combat-log lines ("retired", "destroyed",
"cannot launch"), verified against real logs from the `chat-log-recorder`
mod. If auto-launch ever drifts out of sync, check those logs first.
