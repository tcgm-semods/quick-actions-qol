"""Quick Actions QoL:

1. Makes the Quick Actions (hotbar) panel draggable by its header.
2. Auto-launches fighters (single cargo fighters + fabricator wings) while
   the player is "in combat". Each fighter/fabricator hotbar slot gets its
   own small lock badge in its corner, toggled independently -- only the
   first such slot ever seen is locked on by default. Whenever any such
   slot exists, a small "FIGHTERS: n" (or "n/cap" once capacity is
   discovered) readout is drawn in the panel's left margin, so the
   bookkeeping estimate below can be sanity-checked against reality at a
   glance.

   The game exposes no live "currently deployed fighter count" anywhere
   client-side, so this mod tracks it itself. The increment happens
   inside a patch on _hotbar_activate_slot -- the single entry point the
   game uses for every input source that can trigger a hotbar action
   (number-key press, manual mouse click, and this mod's own auto-launch
   call alike, per that method's own docstring) -- rather than only
   inside the auto-launch code, so a fighter or wing launched by hand is
   tracked exactly the same as one this mod launches itself. Each launch
   optimistically increments the local estimate by 1 -- a launched wing
   counts as one slot, same as a single fighter, since the server tracks
   a whole wing as one entity (its own alive/dead bitmask), not one slot
   per fighter inside it. A chat_log line containing "cannot" means that
   launch was refused -- undo the increment and lock in the corrected
   total as the discovered capacity. A line containing "retired" or
   "destroyed" means a slot freed up (a wing returned on its own, or was
   wiped out in combat), so decrement the estimate either way. Once the
   estimate reaches a discovered capacity, further attempts pause until
   one of those two lines drops it back down. No arbitrary timer.

   These three keywords were confirmed against a real play session
   recorded by the separate chat-log-recorder mod, which is how
   "destroyed" was found in the first place: the actual line is
   "Liberty Dart destroyed." (the fighter type's display name, not the
   word "fighter"), so it would have been invisible to a filter that
   only looked for "fighter". There's still no guarantee these three
   cover every possible fighter-fate message -- keep chat-log-recorder
   installed and grep its logs after future sessions if auto-launch ever
   seems to drift out of sync with reality.

Draggability needs two coordinated patches, because the panel is drawn
through a retained/cached GPU layer whose cache key lives outside
_draw_hotbar itself:

  * _draw_hotbar (render_mixin.py) computes its panel position from
    ctx.win_w/ctx.win_h via a fixed centered-bottom formula. We wrap the
    ctx it receives with a proxy that fakes win_w/win_h just enough to
    make that formula land on our own stored position, without touching
    the dozens of lines of slot-rendering logic after it.
  * That positioning only takes effect on frames where the retained layer
    "hud:hotbar" actually redraws, which is gated by a state_key computed
    far away in Client.py's main render loop -- a key that has no idea
    our drag position changed. So _draw_retained_gpu_layer itself is
    patched to fold our drag state into the key, but only for that one
    cache_id, leaving every other retained layer untouched.

Click handling for both the drag and the toggle is owned entirely by this
mod via the client.event hook, since dragging a HUD panel and toggling a
mod-added button have no native equivalent to piggyback on.
"""
import os
import time

_icons = {}
_install_path = [None]

_original_draw_hud_launchers = None  # unused here, kept for symmetry/clarity
_original_draw_hotbar = None
_original_draw_retained_gpu_layer = None
_original_register_hit = None
_original_chat_log = None
_original_hotbar_activate_slot = None
_patched_class = None

# ── Drag state ───────────────────────────────────────────────────────────
_custom_pos = [None]      # (x, y) once user has dragged; None = default anchor
_dragging = [False]
_drag_offset = [(0, 0)]

# ── Auto-launch state ────────────────────────────────────────────────────
# Per-slot lock: {slot_idx: bool}. Only the first fighter/fabricator slot
# ever seen defaults to True; every other slot defaults to False.
_slot_enabled = {}
_defaults_initialized = [False]
_last_hit_time = [0.0]
_last_launch_attempt = [0.0]
_COMBAT_WINDOW_S = 12.0
_LAUNCH_CHECK_INTERVAL_S = 0.25

# The server exposes no live "currently deployed fighter count" anywhere
# in _fabricator_charge_states -- that dict only carries charge readiness
# (quantity/charged/charge_capacity) and static per-type config
# (deployment_count is fixed "fighters per wing", not a live count, per
# the tooltip text "deploys N fighter(s) per wing"). So the only way to
# know how many are actually out is to track it ourselves from what we
# send, corrected by the two signals the server does give us in chat:
#   - a line containing "cannot"  -> that launch was refused: we were at
#     capacity, so treat our current (pre-launch) estimate as the
#     discovered cap and undo the optimistic increment for it.
#   - a line containing "retired" -> a fighter aged out / returned,
#     freeing one slot: decrement the estimate.
# This also means we no longer need (or want) an arbitrary timed backoff:
# once _known_capacity is discovered, we simply stop attempting once our
# estimate reaches it, and resume the instant a "retired" line drops the
# estimate back below it -- no guessing at how long fighters stay out.
_active_fighter_estimate = [0]
_known_capacity = [None]
_mod_logger = [None]

# Populated every draw call so the event handler can hit-test against the
# exact rects currently on screen.
_layout_rects = {}
_slot_badge_rects = {}


def _qualifying_slots(host):
    hotbar = getattr(host, "_hotbar", None) or []
    for slot_idx, binding in enumerate(hotbar):
        if binding and binding.get("item_category") in ("fighter", "fabricator"):
            yield slot_idx


def _ensure_slot_defaults(host):
    qualifying = list(_qualifying_slots(host))
    if not _defaults_initialized[0] and qualifying:
        _slot_enabled[qualifying[0]] = True
        _defaults_initialized[0] = True
    for slot_idx in qualifying:
        _slot_enabled.setdefault(slot_idx, False)


def _load_icons(pygame, install_path):
    icons_dir = os.path.join(str(install_path), "mod", "quick_actions_qol", "icons")
    for name in ("zap_on", "zap_off"):
        path = os.path.join(icons_dir, f"{name}.png")
        try:
            _icons[name] = pygame.image.load(path).convert_alpha()
        except Exception:
            _icons[name] = None


class _CtxProxy:
    """Wraps a draw ctx, overriding win_w/win_h and delegating everything
    else (screen, dt, ...) to the real object."""

    def __init__(self, real, win_w, win_h):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "win_w", win_w)
        object.__setattr__(self, "win_h", win_h)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


def _hotbar_panel_metrics(self):
    """Reproduce _draw_hotbar's own size formula (render_mixin.py) so we can
    predict panel_w/panel_h and per-slot rects without touching its
    internals. Falls back harmlessly (mod just stops repositioning /
    misplaces badges) if these constants drift."""
    slot = self._s(48)
    gap = self._s(4)
    pad = self._s(7)
    header_h = self._s(15)
    content_gap = self._s(6)
    bottom_pad = self._s(3)
    slots = self.HOTBAR_SLOTS
    total_w = slot * slots + gap * (slots - 1)
    panel_w = total_w + pad * 2
    panel_h = header_h + content_gap + slot + pad
    return {
        "slot": slot, "gap": gap, "pad": pad, "header_h": header_h,
        "content_gap": content_gap, "bottom_pad": bottom_pad,
        "panel_w": panel_w, "panel_h": panel_h,
    }


def _resolve_hotbar_pos(self, win_w, win_h):
    m = _hotbar_panel_metrics(self)
    default_x = (win_w - m["panel_w"]) // 2
    default_y = win_h - m["panel_h"] - m["bottom_pad"]
    if _custom_pos[0] is None:
        x, y = default_x, default_y
    else:
        x, y = _custom_pos[0]
        x = max(0, min(x, max(0, win_w - m["panel_w"])))
        y = max(0, min(y, max(0, win_h - m["panel_h"])))
    return x, y, m


def _slot_rect(pygame, pos_x, pos_y, m, slot_idx):
    x0 = pos_x + m["pad"] + slot_idx * (m["slot"] + m["gap"])
    y0 = pos_y + m["header_h"] + m["content_gap"]
    return pygame.Rect(x0, y0, m["slot"], m["slot"])


def _make_draw_hotbar(pygame, original):
    def patched(self, ctx, *, draw_animation=True, tick_press=True):
        _ensure_slot_defaults(self)

        real_win_w, real_win_h = ctx.win_w, ctx.win_h
        pos_x, pos_y, m = _resolve_hotbar_pos(self, real_win_w, real_win_h)

        virtual_w = pos_x * 2 + m["panel_w"]
        virtual_h = pos_y + m["panel_h"] + m["bottom_pad"]
        proxy = _CtxProxy(ctx, virtual_w, virtual_h)

        result = original(
            self, proxy, draw_animation=draw_animation, tick_press=tick_press)

        # Per-slot lock badge: a small corner tag on every fighter/fabricator
        # slot, so each one can be auto-launch-locked independently instead
        # of one panel-wide switch.
        screen = ctx.screen
        mouse = pygame.mouse.get_pos()
        badge_size = self._s(14)
        _slot_badge_rects.clear()
        hovered_badge = None

        for slot_idx in _qualifying_slots(self):
            slot_rect = _slot_rect(pygame, pos_x, pos_y, m, slot_idx)
            badge_rect = pygame.Rect(
                slot_rect.right - badge_size - self._s(1),
                slot_rect.top + self._s(1),
                badge_size, badge_size)
            enabled = _slot_enabled.get(slot_idx, False)
            hover = badge_rect.collidepoint(mouse)
            fill = (18, 58, 48) if enabled else (18, 24, 32)
            if hover:
                fill = tuple(min(255, c + 14) for c in fill)
            border = (95, 220, 180) if enabled else (70, 84, 100)
            pygame.draw.rect(
                screen, fill, badge_rect, border_radius=self._s(3))
            pygame.draw.rect(
                screen, border, badge_rect, 1, border_radius=self._s(3))
            icon = _icons.get("zap_on" if enabled else "zap_off")
            if icon is not None:
                small = pygame.transform.smoothscale(
                    icon, (badge_size - self._s(4), badge_size - self._s(4)))
                screen.blit(small, small.get_rect(center=badge_rect.center))

            _slot_badge_rects[slot_idx] = badge_rect
            if hover:
                hovered_badge = (slot_idx, badge_rect, enabled)

        if hovered_badge is not None:
            _, badge_rect, enabled = hovered_badge
            label_text = (
                "Auto-Launch: LOCKED ON" if enabled
                else "Auto-Launch: LOCKED OFF")
            font = self._instrument_font(9, bold=True)
            label = font.render(label_text.upper(), True, (228, 237, 250))
            pad_t = self._s(5)
            box = pygame.Rect(
                0, 0, label.get_width() + pad_t * 2,
                label.get_height() + pad_t * 2)
            box.midbottom = (badge_rect.centerx, badge_rect.top - self._s(4))
            box.left = max(self._s(4), min(
                box.left, real_win_w - box.width - self._s(4)))
            pygame.draw.rect(
                screen, (8, 18, 33), box, border_radius=self._s(3))
            pygame.draw.rect(
                screen, (56, 88, 138), box, 1, border_radius=self._s(3))
            screen.blit(label, (box.x + pad_t, box.y + pad_t))

        # Live readout of the fighter-slot bookkeeping (see _make_chat_log),
        # in the panel's left margin, so the estimate/known-capacity can be
        # sanity-checked against reality at a glance instead of only via
        # the recorded chat log.
        qualifying = list(_qualifying_slots(self))
        if qualifying:
            cap = _known_capacity[0]
            count_text = (
                f"FIGHTERS: {_active_fighter_estimate[0]}/{cap}"
                if cap is not None
                else f"FIGHTERS: {_active_fighter_estimate[0]}")
            count_font = self._instrument_font(10, bold=True)
            count_label = count_font.render(
                count_text, True, (185, 215, 250))
            pad_c = self._s(6)
            count_box = pygame.Rect(
                0, 0, count_label.get_width() + pad_c * 2,
                count_label.get_height() + pad_c * 2)
            count_box.midright = (
                pos_x - self._s(6), pos_y + m["panel_h"] // 2)
            count_box.left = max(self._s(4), count_box.left)
            pygame.draw.rect(
                screen, (8, 18, 33), count_box, border_radius=self._s(4))
            pygame.draw.rect(
                screen, (56, 88, 138), count_box, 1, border_radius=self._s(4))
            screen.blit(count_label, (count_box.x + pad_c, count_box.y + pad_c))

        _layout_rects["panel"] = pygame.Rect(
            pos_x, pos_y, m["panel_w"], m["panel_h"])
        _layout_rects["header"] = pygame.Rect(
            pos_x, pos_y, m["panel_w"], m["header_h"])

        return result

    return patched


def _make_draw_retained_gpu_layer(original):
    def patched(self, cache_id, ctx, state_key, draw):
        if cache_id == "hud:hotbar":
            state_key = state_key + (
                _custom_pos[0], _dragging[0],
                tuple(sorted(_slot_enabled.items())),
                _active_fighter_estimate[0], _known_capacity[0])
        return original(self, cache_id, ctx, state_key, draw)
    return patched


def _make_register_hit(original):
    def patched(self, hit):
        result = original(self, hit)
        try:
            tid = hit.get("target_id")
            me = self._find_my_entity()
            if me is not None and me.get("player_id") == tid:
                _last_hit_time[0] = time.monotonic()
        except Exception:
            pass
        return result
    return patched


def _make_hotbar_activate_slot(original):
    def patched(self, slot_idx):
        # The single entry point for every input source that can trigger a
        # hotbar action (number-key press, manual mouse click on a slot,
        # and this mod's own auto-launch call) -- see its docstring in
        # Client.py. Patching here instead of only counting this mod's own
        # calls means a fighter/wing launched by hand is tracked too, not
        # just ones auto-launch triggers.
        hotbar = getattr(self, "_hotbar", None) or []
        binding = hotbar[slot_idx] if 0 <= slot_idx < len(hotbar) else None
        will_launch = False
        if binding:
            category = binding.get("item_category")
            if category == "fighter":
                will_launch = self._hotbar_find_cargo_key(binding) is not None
            elif category == "fabricator":
                fab_type = str(binding.get("item_type") or "")
                state = self._fabricator_charge_states.get(fab_type)
                will_launch = (
                    state is not None and state.get("status") == "ready")
        result = original(self, slot_idx)
        if will_launch:
            _active_fighter_estimate[0] += 1
        return result
    return patched


def _make_chat_log(original):
    def patched(self, text, colour=(120, 150, 200)):
        result = original(self, text, colour)
        try:
            lower = str(text).lower()
            # Neither "cannot" nor "retired" is confirmed to be the whole
            # vocabulary of fighter-fate messages -- there was no way to
            # discover the rest offline, since chat_log() only ever
            # appends to the in-game UI list and never reaches the debug
            # logger. So record every fighter-related line this mod's own
            # log (Mods/mods.log) during play; if auto-launch keeps
            # attempting when it shouldn't (or backs off when it
            # shouldn't), that log is where the missed wording will show
            # up, and the keyword list below can be extended from it.
            if ("fighter" in lower or "destroyed" in lower
                    or "wing" in lower) and _mod_logger[0] is not None:
                _mod_logger[0].info("chat_log fighter-related line: %r", text)
            if "cannot" in lower:
                # The launch we just optimistically counted (by 1 -- a
                # launched wing is still one slot, same as a single
                # fighter; the server tracks a whole wing as one entity
                # with its own internal alive/dead mask, not N slots) was
                # refused. Undo that increment and lock in the corrected
                # total as the discovered cap, so future attempts stop
                # before hitting it again instead of re-discovering it by
                # spamming rejections.
                _active_fighter_estimate[0] = max(
                    0, _active_fighter_estimate[0] - 1)
                _known_capacity[0] = _active_fighter_estimate[0]
            elif "retired" in lower or "destroyed" in lower:
                # "retired" -- a wing survived and returned on its own.
                # "destroyed" -- a wing was wiped out in combat (confirmed
                # via a live-play log: "Liberty Dart destroyed." fires
                # without "fighter" anywhere in the text, so it would have
                # been missed by keyword matching on "fighter" alone).
                # Either way the slot it occupied is free again.
                _active_fighter_estimate[0] = max(
                    0, _active_fighter_estimate[0] - 1)
        except Exception:
            pass
        return result
    return patched


def _in_combat(host):
    now = time.monotonic()
    if now - _last_hit_time[0] < _COMBAT_WINDOW_S:
        return True
    if getattr(host, "_targeted_pid", None) is not None:
        return True
    if getattr(host, "_targeted_npc_id", None) is not None:
        return True
    return False


def _try_auto_launch(host):
    _ensure_slot_defaults(host)
    now = time.monotonic()
    if now - _last_launch_attempt[0] < _LAUNCH_CHECK_INTERVAL_S:
        return
    if not _in_combat(host):
        return
    _last_launch_attempt[0] = now

    hotbar = getattr(host, "_hotbar", None)
    if not hotbar:
        return
    for slot_idx, binding in enumerate(hotbar):
        if not binding or not _slot_enabled.get(slot_idx, False):
            continue
        cap = _known_capacity[0]
        if cap is not None and _active_fighter_estimate[0] >= cap:
            continue
        category = binding.get("item_category")
        if category == "fighter":
            if host._hotbar_find_cargo_key(binding) is None:
                continue
            host._hotbar_activate_slot(slot_idx)
        elif category == "fabricator":
            fab_type = str(binding.get("item_type") or "")
            state = host._fabricator_charge_states.get(fab_type)
            if state is None or state.get("status") != "ready":
                continue
            host._hotbar_activate_slot(slot_idx)
        # The estimate itself is incremented inside the patched
        # _hotbar_activate_slot (see _make_hotbar_activate_slot), not
        # here -- that's the single entry point the game uses for every
        # input source (number-key press, manual hotbar click, and this
        # auto-launch call alike), so incrementing there instead of here
        # means a manually-launched fighter/wing is counted too, not just
        # ones this mod triggers itself.


def _on_frame_begin(host, render_target):
    _try_auto_launch(host)


def _on_event(host, pygame, event, screen):
    if (getattr(host, "_disconnected", False)
            or getattr(host, "_esc_menu_open", False)
            or getattr(host, "_options_open", False)):
        return

    if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
        for slot_idx, badge_rect in _slot_badge_rects.items():
            if badge_rect.collidepoint(event.pos):
                _slot_enabled[slot_idx] = not _slot_enabled.get(slot_idx, False)
                return
        header_rect = _layout_rects.get("header")
        panel_rect = _layout_rects.get("panel")
        if (header_rect is not None and panel_rect is not None
                and header_rect.collidepoint(event.pos)):
            _dragging[0] = True
            _drag_offset[0] = (
                event.pos[0] - panel_rect.x, event.pos[1] - panel_rect.y)
        return

    if event.type == pygame.MOUSEMOTION and _dragging[0]:
        dx, dy = _drag_offset[0]
        _custom_pos[0] = (event.pos[0] - dx, event.pos[1] - dy)
        return

    if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
        _dragging[0] = False


def _on_startup(host, pygame, screen):
    global _original_draw_hotbar, _original_draw_retained_gpu_layer
    global _original_register_hit, _original_chat_log, _patched_class
    global _original_hotbar_activate_slot

    _load_icons(pygame, _install_path[0])

    cls = type(host)
    _patched_class = cls
    _original_draw_hotbar = cls._draw_hotbar
    cls._draw_hotbar = _make_draw_hotbar(pygame, _original_draw_hotbar)

    _original_draw_retained_gpu_layer = cls._draw_retained_gpu_layer
    cls._draw_retained_gpu_layer = _make_draw_retained_gpu_layer(
        _original_draw_retained_gpu_layer)

    _original_register_hit = cls.register_hit
    cls.register_hit = _make_register_hit(_original_register_hit)

    _original_chat_log = cls.chat_log
    cls.chat_log = _make_chat_log(_original_chat_log)

    _original_hotbar_activate_slot = cls._hotbar_activate_slot
    cls._hotbar_activate_slot = _make_hotbar_activate_slot(
        _original_hotbar_activate_slot)


def _on_shutdown(**_kwargs):
    if _patched_class is None:
        return
    if _original_draw_hotbar is not None:
        _patched_class._draw_hotbar = _original_draw_hotbar
    if _original_draw_retained_gpu_layer is not None:
        _patched_class._draw_retained_gpu_layer = _original_draw_retained_gpu_layer
    if _original_register_hit is not None:
        _patched_class.register_hit = _original_register_hit
    if _original_chat_log is not None:
        _patched_class.chat_log = _original_chat_log
    if _original_hotbar_activate_slot is not None:
        _patched_class._hotbar_activate_slot = _original_hotbar_activate_slot


def apply(api):
    _install_path[0] = api.install_path
    _mod_logger[0] = api.logger
    api.on("client.startup", _on_startup)
    api.on("client.frame.begin", _on_frame_begin)
    api.on("client.event", _on_event)
    api.on("loader.shutdown", _on_shutdown)
    api.logger.info("quick-actions-qol ready, waiting for client.startup")
