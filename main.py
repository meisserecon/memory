"""
Memory Match - a tiny pygame prototype.

Flip cards to find all matching pairs. Finish in few enough moves to
earn a spot on the high score board: type your name and it is saved
to a highscores.json file next to this script. When the game runs in
a web browser (see server.py), the board is shared between all
players via the server instead.

Controls: everything works by mouse click or touch tap (pick a
difficulty, flip cards, use the on-screen Menu / Restart / Save
buttons). A physical keyboard works too: keys 1-4 pick a difficulty,
H opens the high score board, R restarts, Esc for menu / quit.
"""

import asyncio
import json
import math
import random
import sys
from array import array
from pathlib import Path

import pygame

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_WIDTH = 640
WINDOW_HEIGHT = 760
HUD_HEIGHT = 70          # top bar with moves / timer
MARGIN = 20              # space around the board
GAP = 12                 # space between cards

FPS = 60
MISMATCH_DELAY_MS = 900  # how long a wrong pair stays visible before flipping back
FLIP_DURATION_MS = 250   # length of the card flip animation

MAX_HIGHSCORES = 10      # entries kept per difficulty
MAX_NAME_LENGTH = 12     # longest name allowed on the high score board
HIGHSCORE_PATH = Path(__file__).with_name("highscores.json")

IS_WEB = sys.platform == "emscripten"  # True when running in a browser (pygbag)
API_BASE = "/api/scores"               # shared leaderboard endpoint (same origin)
LOCALSTORAGE_KEY = "memory_match_highscores"  # per-browser fallback board

# (menu label, grid columns, grid rows) - number of cards must be even
DIFFICULTIES = [
    ("Easy", 4, 4),      # 16 cards, 8 pairs
    ("Medium", 6, 4),    # 24 cards, 12 pairs
    ("Hard", 6, 6),      # 36 cards, 18 pairs
    ("Expert", 10, 10),  # 100 cards, 50 pairs
]

# Colors (R, G, B)
BACKGROUND = (24, 26, 38)
CARD_BACK = (70, 73, 96)
CARD_BACK_BORDER = (120, 124, 150)
CARD_MATCHED_BORDER = (255, 255, 255)
BUTTON = (70, 73, 96)
BUTTON_HOVER = (95, 99, 128)
TEXT_LIGHT = (245, 245, 245)
TEXT_DARK = (30, 30, 30)
BANNER_BG = (0, 0, 0, 170)  # semi-transparent overlay

# Each pair gets a number and one of these colors. With more pairs than
# colors the colors repeat - the number is what identifies a pair.
PAIR_COLORS = [
    (239, 83, 80),    # red
    (255, 167, 38),   # orange
    (255, 213, 79),   # yellow
    (102, 187, 106),  # green
    (38, 166, 154),   # teal
    (66, 165, 245),   # blue
    (171, 71, 188),   # purple
    (236, 64, 122),   # pink
    (141, 110, 99),   # brown
    (120, 144, 156),  # blue gray
    (255, 112, 67),   # deep orange
    (139, 195, 74),   # light green
    (77, 208, 225),   # cyan
    (92, 107, 192),   # indigo
    (186, 104, 200),  # light purple
    (240, 98, 146),   # light pink
    (212, 225, 87),   # lime
    (255, 202, 40),   # amber
]


# ---------------------------------------------------------------------------
# Board layout and deck
# ---------------------------------------------------------------------------

class Card:
    """One card on the board."""

    def __init__(self, value, color, rect):
        self.value = value      # number shown on the front
        self.color = color      # front color (shared by both cards of a pair)
        self.rect = rect        # position and size on screen
        self.face_up = False
        self.matched = False
        self._flip_start = None  # timestamp when the flip animation began (None = idle)
        self._old_face = False   # face shown during the first half of the flip

    def start_flip(self, now):
        """Begin the flip animation; face_up changes immediately for game logic."""
        self._old_face = self.face_up
        self.face_up = not self.face_up
        self._flip_start = now

    def flip_view(self, now):
        """Return (scale_x, face_up_to_draw) for the current animation frame.

        The card squashes horizontally to zero width, swaps face at the
        midpoint, then grows back - a simple but convincing flip illusion.
        """
        if self._flip_start is None:
            return 1.0, self.face_up
        t = (now - self._flip_start) / FLIP_DURATION_MS
        if t >= 1.0:
            self._flip_start = None  # animation finished
            return 1.0, self.face_up
        if t < 0.5:
            return 1.0 - t * 2, self._old_face  # shrinking, old face
        return (t - 0.5) * 2, self.face_up      # growing, new face


def board_layout(cols, rows):
    """Pick the biggest card size that fits the window, and center the board."""
    avail_w = WINDOW_WIDTH - MARGIN * 2
    avail_h = WINDOW_HEIGHT - HUD_HEIGHT - MARGIN * 2
    card_size = min((avail_w - (cols - 1) * GAP) // cols,
                    (avail_h - (rows - 1) * GAP) // rows)
    board_w = cols * card_size + (cols - 1) * GAP
    board_h = rows * card_size + (rows - 1) * GAP
    offset_x = (WINDOW_WIDTH - board_w) // 2
    offset_y = HUD_HEIGHT + MARGIN + (avail_h - board_h) // 2
    return card_size, offset_x, offset_y


def build_deck(cols, rows):
    """Create shuffled pairs and lay them out on the grid."""
    pair_count = cols * rows // 2
    pairs = list(range(pair_count)) * 2  # each pair id appears twice
    random.shuffle(pairs)

    card_size, offset_x, offset_y = board_layout(cols, rows)

    cards = []
    for i, pair_id in enumerate(pairs):
        col = i % cols
        row = i // cols
        x = offset_x + col * (card_size + GAP)
        y = offset_y + row * (card_size + GAP)
        rect = pygame.Rect(x, y, card_size, card_size)
        cards.append(Card(pair_id + 1, PAIR_COLORS[pair_id % len(PAIR_COLORS)], rect))
    return cards


def card_at(cards, pos):
    """Return the card under the mouse position, or None."""
    for card in cards:
        if card.rect.collidepoint(pos):
            return card
    return None


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------

class Game:
    """Holds everything about the current round."""

    def __init__(self, difficulty, sounds=None):
        self.difficulty = difficulty  # (label, cols, rows)
        self.sounds = sounds or {}    # name -> Sound; empty = silent
        self.reset()

    def _play(self, name):
        """Play a sound effect if audio is available."""
        sound = self.sounds.get(name)
        if sound:
            sound.play()

    def reset(self):
        """Start a fresh round (also used when pressing R)."""
        _label, cols, rows = self.difficulty
        self.cards = build_deck(cols, rows)
        self.first_card = None     # first card of the current turn
        self.second_card = None    # second card of the current turn
        self.mismatch_at = None    # timestamp of a wrong pair (None = not showing one)
        self.moves = 0
        self.start_ticks = pygame.time.get_ticks()
        self.won = False
        self.final_time_ms = 0

    def handle_click(self, pos):
        """Flip the clicked card and apply the matching rules."""
        # Ignore clicks after winning or while a wrong pair is being shown.
        if self.won or self.mismatch_at is not None:
            return

        card = card_at(self.cards, pos)
        if card is None or card.face_up:
            return

        card.start_flip(pygame.time.get_ticks())

        if self.first_card is None:
            # This was the first card of the turn - wait for the second.
            self.first_card = card
            return

        # This was the second card - the turn is complete.
        self.second_card = card
        self.moves += 1

        if self.first_card.value == self.second_card.value:
            # Match! Both cards stay face up.
            self._play("match")
            self.first_card.matched = True
            self.second_card.matched = True
            self.first_card = None
            self.second_card = None

            if all(c.matched for c in self.cards):
                self.won = True
                self.final_time_ms = pygame.time.get_ticks() - self.start_ticks
        else:
            # No match - remember when, update() will flip them back.
            self._play("mismatch")
            self.mismatch_at = pygame.time.get_ticks()

    def update(self):
        """Flip a wrong pair back down once the delay has passed.

        This uses a non-blocking timer (comparing timestamps) instead of
        time.sleep(), so the window stays responsive the whole time.
        """
        if self.mismatch_at is None:
            return
        now = pygame.time.get_ticks()
        if now - self.mismatch_at >= MISMATCH_DELAY_MS:
            self.first_card.start_flip(now)
            self.second_card.start_flip(now)
            self.first_card = None
            self.second_card = None
            self.mismatch_at = None

    def elapsed_ms(self):
        """Time played so far; frozen once the game is won."""
        if self.won:
            return self.final_time_ms
        return pygame.time.get_ticks() - self.start_ticks


# ---------------------------------------------------------------------------
# High score storage
#
# On the desktop the board is kept in a small JSON file next to this script.
# In the browser it lives on the web server (shared between all players);
# if the server cannot be reached, the browser's localStorage is used as a
# per-browser fallback so scores still persist between visits.
# ---------------------------------------------------------------------------

def empty_highscores():
    """One empty list per difficulty."""
    return {label: [] for label, _cols, _rows in DIFFICULTIES}


def score_key(entry):
    """Fewer moves is better; ties go to the faster time."""
    return entry["moves"], entry["time_ms"]


def normalize_highscores(data):
    """Keep only known difficulties and well-formed entries, sorted and capped."""
    scores = empty_highscores()
    if not isinstance(data, dict):
        return scores
    for label in scores:
        for entry in data.get(label, []):
            try:
                scores[label].append({
                    "name": str(entry["name"]),
                    "moves": int(entry["moves"]),
                    "time_ms": int(entry["time_ms"]),
                })
            except (KeyError, TypeError, ValueError):
                continue  # skip malformed entries
        scores[label].sort(key=score_key)
        del scores[label][MAX_HIGHSCORES:]
    return scores


def load_highscores():
    """Read the local high score file; start fresh if it is missing or corrupt."""
    try:
        data = json.loads(HIGHSCORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    return normalize_highscores(data)


def save_highscores(scores):
    """Write the scores back to disk; ignore errors (e.g. read-only folder)."""
    try:
        HIGHSCORE_PATH.write_text(json.dumps(scores, indent=2), encoding="utf-8")
    except OSError:
        pass


def qualifies_for_highscore(scores, label, moves, time_ms):
    """True if the result would make it onto the board."""
    entries = scores[label]
    if len(entries) < MAX_HIGHSCORES:
        return True
    return (moves, time_ms) < score_key(entries[-1])


def add_highscore(scores, label, name, moves, time_ms):
    """Insert a new entry into a board in memory, keep only the best ones."""
    entries = scores[label]
    entries.append({"name": name, "moves": moves, "time_ms": time_ms})
    entries.sort(key=score_key)
    del entries[MAX_HIGHSCORES:]


# --- browser-only helpers (pygbag provides the "platform" shim) -------------

async def _fetch_text(url):
    """Download a text resource over HTTP (browser only)."""
    import platform
    async with platform.fopen(url, "r") as response:
        return response.read()


def url_quote(text):
    """Minimal percent-encoding for query string values (no urllib needed)."""
    return "".join(f"%{byte:02X}" for byte in text.encode("utf-8"))


def _local_storage_load():
    """Fallback board stored in the browser itself (not shared)."""
    import platform
    try:
        raw = platform.window.localStorage.getItem(LOCALSTORAGE_KEY)
        return normalize_highscores(json.loads(raw) if raw else None)
    except Exception:
        return empty_highscores()


def _local_storage_save(scores):
    import platform
    try:
        platform.window.localStorage.setItem(LOCALSTORAGE_KEY, json.dumps(scores))
    except Exception:
        pass


async def load_scores():
    """Return (scores, online); 'online' tells if the shared server board works."""
    if not IS_WEB:
        return load_highscores(), False
    try:
        data = json.loads(await _fetch_text(API_BASE))
        return normalize_highscores(data), True
    except Exception:
        return _local_storage_load(), False


async def submit_score(scores, online, label, name, moves, time_ms):
    """Add an entry and persist it. Returns (scores, online) - the web version
    may fall back to per-browser storage if the server cannot be reached."""
    if not IS_WEB:
        add_highscore(scores, label, name, moves, time_ms)
        save_highscores(scores)
        return scores, online
    if online:
        query = (f"?name={url_quote(name)}&difficulty={url_quote(label)}"
                 f"&moves={moves}&time_ms={time_ms}")
        try:
            data = json.loads(await _fetch_text(API_BASE + query))
            return normalize_highscores(data), True
        except Exception:
            online = False  # server went away - fall through to local storage
    add_highscore(scores, label, name, moves, time_ms)
    _local_storage_save(scores)
    return scores, online


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def format_time(ms):
    """90000 -> '1:30'"""
    total_seconds = ms // 1000
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def draw_button(screen, rect, label, font, mouse_pos):
    """One tappable button; brightens under the mouse (no hover on touch)."""
    color = BUTTON_HOVER if rect.collidepoint(mouse_pos) else BUTTON
    pygame.draw.rect(screen, color, rect, border_radius=10)
    pygame.draw.rect(screen, CARD_BACK_BORDER, rect, width=3, border_radius=10)
    text = font.render(label, True, TEXT_LIGHT)
    screen.blit(text, text.get_rect(center=rect.center))


def text_color_for(bg):
    """Pick dark or light text depending on how bright the background is."""
    r, g, b = bg
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    return TEXT_DARK if brightness > 150 else TEXT_LIGHT


def card_fonts(card_size):
    """Fonts scaled to the card size (big cards get big numbers)."""
    number_font = pygame.font.SysFont("arial", max(20, int(card_size * 0.45)), bold=True)
    back_font = pygame.font.SysFont("arial", max(18, int(card_size * 0.40)), bold=True)
    return number_font, back_font


def render_card_face(card, number_font, back_font, face_up):
    """Draw one side of the card onto a new surface and return it."""
    surface = pygame.Surface(card.rect.size, pygame.SRCALPHA)
    local_rect = surface.get_rect()
    if face_up:
        # Front of the card: pair color + number.
        pygame.draw.rect(surface, card.color, local_rect, border_radius=10)
        border_color = CARD_MATCHED_BORDER if card.matched else CARD_BACK_BORDER
        pygame.draw.rect(surface, border_color, local_rect, width=3, border_radius=10)
        text = number_font.render(str(card.value), True, text_color_for(card.color))
        surface.blit(text, text.get_rect(center=local_rect.center))
    else:
        # Back of the card: plain gray with a question mark.
        pygame.draw.rect(surface, CARD_BACK, local_rect, border_radius=10)
        pygame.draw.rect(surface, CARD_BACK_BORDER, local_rect, width=3, border_radius=10)
        text = back_font.render("?", True, CARD_BACK_BORDER)
        surface.blit(text, text.get_rect(center=local_rect.center))
    return surface


def draw_card(screen, card, number_font, back_font, now):
    """Draw the card, squashing it horizontally while the flip animation runs."""
    scale_x, face_up = card.flip_view(now)
    face = render_card_face(card, number_font, back_font, face_up)
    if scale_x >= 1.0:
        screen.blit(face, card.rect.topleft)
        return
    width = max(2, int(card.rect.width * scale_x))
    scaled = pygame.transform.smoothscale(face, (width, card.rect.height))
    screen.blit(scaled, scaled.get_rect(center=card.rect.center))


def hud_buttons():
    """Small tappable Menu / Restart buttons in the top bar (R / Esc still work)."""
    y = (HUD_HEIGHT - 38) // 2
    menu = pygame.Rect(0, y, 100, 38)
    restart = pygame.Rect(0, y, 110, 38)
    total = menu.width + 12 + restart.width
    menu.left = (WINDOW_WIDTH - total) // 2
    restart.left = menu.right + 12
    return [("menu", menu), ("restart", restart)]


def draw_hud(screen, game, buttons, hud_font, button_font, mouse_pos):
    moves_text = hud_font.render(f"Moves: {game.moves}", True, TEXT_LIGHT)
    screen.blit(moves_text, (MARGIN, 18))

    time_text = hud_font.render(f"Time: {format_time(game.elapsed_ms())}", True, TEXT_LIGHT)
    screen.blit(time_text, time_text.get_rect(topright=(WINDOW_WIDTH - MARGIN, 18)))

    for action, rect in buttons:
        draw_button(screen, rect, "Restart" if action == "restart" else "Menu",
                    button_font, mouse_pos)


def win_banner_buttons():
    """Tappable Play Again / Menu buttons on the win banner."""
    again = pygame.Rect(0, 0, 150, 50)
    menu = pygame.Rect(0, 0, 110, 50)
    total = again.width + 16 + menu.width
    again.left = (WINDOW_WIDTH - total) // 2
    again.top = WINDOW_HEIGHT // 2 + 45
    menu.left = again.right + 16
    menu.top = again.top
    return [("again", again), ("menu", menu)]


def draw_win_banner(screen, game, buttons, banner_font, hud_font, button_font, mouse_pos):
    overlay = pygame.Surface((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.SRCALPHA)
    overlay.fill(BANNER_BG)
    screen.blit(overlay, (0, 0))

    center_x = WINDOW_WIDTH // 2
    center_y = WINDOW_HEIGHT // 2

    title = banner_font.render("You Won!", True, TEXT_LIGHT)
    screen.blit(title, title.get_rect(center=(center_x, center_y - 50)))

    stats = hud_font.render(
        f"Solved in {game.moves} moves, {format_time(game.final_time_ms)}", True, TEXT_LIGHT
    )
    screen.blit(stats, stats.get_rect(center=(center_x, center_y + 10)))

    for action, rect in buttons:
        draw_button(screen, rect, "Play Again" if action == "again" else "Menu",
                    button_font, mouse_pos)


# ---------------------------------------------------------------------------
# Menu screen
# ---------------------------------------------------------------------------

def menu_buttons():
    """One centered button per difficulty; returns [(rect, difficulty_index)]."""
    button_w, button_h = 320, 64
    gap_y = 16
    start_y = 270
    buttons = []
    for i in range(len(DIFFICULTIES)):
        x = (WINDOW_WIDTH - button_w) // 2
        y = start_y + i * (button_h + gap_y)
        buttons.append((pygame.Rect(x, y, button_w, button_h), i))
    return buttons


def highscores_button():
    """The menu button that opens the high score board."""
    return pygame.Rect((WINDOW_WIDTH - 320) // 2, 582, 320, 56)


def draw_menu(screen, buttons, hs_button, mouse_pos, banner_font, small_font, hud_font):
    title = banner_font.render("Memory Match", True, TEXT_LIGHT)
    screen.blit(title, title.get_rect(center=(WINDOW_WIDTH // 2, 150)))

    subtitle = small_font.render("Find all the pairs!", True, CARD_BACK_BORDER)
    screen.blit(subtitle, subtitle.get_rect(center=(WINDOW_WIDTH // 2, 210)))

    for rect, i in buttons:
        # Brighten the button under the mouse.
        color = BUTTON_HOVER if rect.collidepoint(mouse_pos) else BUTTON
        pygame.draw.rect(screen, color, rect, border_radius=12)
        pygame.draw.rect(screen, CARD_BACK_BORDER, rect, width=3, border_radius=12)

        label, cols, rows = DIFFICULTIES[i]
        text = hud_font.render(f"{i + 1} - {label} ({cols}x{rows})", True, TEXT_LIGHT)
        screen.blit(text, text.get_rect(center=rect.center))

    color = BUTTON_HOVER if hs_button.collidepoint(mouse_pos) else BUTTON
    pygame.draw.rect(screen, color, hs_button, border_radius=12)
    pygame.draw.rect(screen, CARD_BACK_BORDER, hs_button, width=3, border_radius=12)
    text = hud_font.render("High Scores", True, TEXT_LIGHT)
    screen.blit(text, text.get_rect(center=hs_button.center))

    hint = small_font.render("Tap or click a button to start - H for high scores",
                             True, CARD_BACK_BORDER)
    screen.blit(hint, hint.get_rect(center=(WINDOW_WIDTH // 2, 680)))


# ---------------------------------------------------------------------------
# High score screens (name entry + board)
# ---------------------------------------------------------------------------

def name_entry_box():
    """The input box rect - also the tap target that opens the mobile keyboard."""
    box = pygame.Rect(0, 0, 340, 56)
    box.center = (WINDOW_WIDTH // 2, WINDOW_HEIGHT // 2)
    return box


def name_entry_buttons():
    """Tappable Save / Skip buttons below the name input box."""
    save = pygame.Rect(0, 0, 120, 46)
    skip = pygame.Rect(0, 0, 120, 46)
    total = save.width + 16 + skip.width
    save.left = (WINDOW_WIDTH - total) // 2
    save.top = WINDOW_HEIGHT // 2 + 110
    skip.left = save.right + 16
    skip.top = save.top
    return [("save", save), ("skip", skip)]


def prompt_for_name(current):
    """Open the browser's text prompt so touch devices get an on-screen
    keyboard. Returns the new name (or the unchanged one on cancel/error)."""
    import platform
    try:
        entered = platform.window.prompt("Your name for the high score board:", current)
    except Exception:
        return current
    if entered is None:
        return current
    return str(entered).strip()[:MAX_NAME_LENGTH]


def draw_name_entry(screen, game, name, now, box, buttons,
                    banner_font, hud_font, small_font, button_font, mouse_pos):
    """Overlay shown after a winning game that made it onto the board."""
    overlay = pygame.Surface((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.SRCALPHA)
    overlay.fill(BANNER_BG)
    screen.blit(overlay, (0, 0))

    center_x = WINDOW_WIDTH // 2
    center_y = WINDOW_HEIGHT // 2

    title = banner_font.render("New High Score!", True, TEXT_LIGHT)
    screen.blit(title, title.get_rect(center=(center_x, center_y - 130)))

    stats = hud_font.render(
        f"{game.difficulty[0]} - {game.moves} moves, {format_time(game.final_time_ms)}",
        True, TEXT_LIGHT,
    )
    screen.blit(stats, stats.get_rect(center=(center_x, center_y - 70)))

    # Input box with a blinking cursor.
    pygame.draw.rect(screen, CARD_BACK, box, border_radius=10)
    pygame.draw.rect(screen, CARD_MATCHED_BORDER, box, width=3, border_radius=10)
    text = hud_font.render(name, True, TEXT_LIGHT)
    text_rect = text.get_rect(center=box.center)
    screen.blit(text, text_rect)
    if (now // 500) % 2 == 0:
        cursor_x = min(text_rect.right + 2, box.right - 10)
        pygame.draw.rect(screen, TEXT_LIGHT,
                         (cursor_x, text_rect.top + 4, 3, text_rect.height - 8))

    hint = small_font.render("Type your name - on touch screens tap the box first",
                             True, TEXT_LIGHT)
    screen.blit(hint, hint.get_rect(center=(center_x, center_y + 48)))

    for action, rect in buttons:
        draw_button(screen, rect, action.capitalize(), button_font, mouse_pos)


def highscore_tab_rects():
    """One small tab button per difficulty on the high score screen."""
    tab_w, tab_h, gap = 130, 46, 14
    total = len(DIFFICULTIES) * tab_w + (len(DIFFICULTIES) - 1) * gap
    x = (WINDOW_WIDTH - total) // 2
    return [(pygame.Rect(x + i * (tab_w + gap), 150, tab_w, tab_h), i)
            for i in range(len(DIFFICULTIES))]


def highscores_back_button():
    """Tappable way back to the menu (Esc still works)."""
    return pygame.Rect(MARGIN, 58, 96, 44)


def draw_highscores(screen, scores, tab, tab_rects, back_button,
                    banner_font, hud_font, small_font, button_font, mouse_pos):
    title = banner_font.render("High Scores", True, TEXT_LIGHT)
    screen.blit(title, title.get_rect(center=(WINDOW_WIDTH // 2, 80)))

    draw_button(screen, back_button, "Back", button_font, mouse_pos)

    # Tabs to switch between difficulties.
    for rect, i in tab_rects:
        selected = i == tab
        color = BUTTON_HOVER if selected else BUTTON
        border = CARD_MATCHED_BORDER if selected else CARD_BACK_BORDER
        pygame.draw.rect(screen, color, rect, border_radius=10)
        pygame.draw.rect(screen, border, rect, width=3, border_radius=10)
        label = small_font.render(DIFFICULTIES[i][0], True, TEXT_LIGHT)
        screen.blit(label, label.get_rect(center=rect.center))

    entries = scores[DIFFICULTIES[tab][0]]
    if not entries:
        empty = small_font.render("No scores yet - be the first!", True, CARD_BACK_BORDER)
        screen.blit(empty, empty.get_rect(center=(WINDOW_WIDTH // 2, 400)))
    else:
        for rank, entry in enumerate(entries):
            y = 230 + rank * 44
            rank_text = hud_font.render(f"{rank + 1}.", True, CARD_BACK_BORDER)
            screen.blit(rank_text, (90, y))
            name_text = hud_font.render(entry["name"], True, TEXT_LIGHT)
            screen.blit(name_text, (150, y))
            moves_text = hud_font.render(f"{entry['moves']} moves", True, TEXT_LIGHT)
            screen.blit(moves_text, (360, y))
            time_text = hud_font.render(format_time(entry["time_ms"]), True, TEXT_LIGHT)
            screen.blit(time_text, time_text.get_rect(topright=(WINDOW_WIDTH - 90, y)))

    hint = small_font.render("1-4 or tap to switch difficulty",
                             True, CARD_BACK_BORDER)
    screen.blit(hint, hint.get_rect(center=(WINDOW_WIDTH // 2, 700)))


# ---------------------------------------------------------------------------
# Sound effects (synthesized in code - no audio files needed)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100


def make_tone(notes, volume=0.35):
    """Build a Sound from a list of (frequency Hz, duration ms) notes.

    Each note is a sine wave with a short fade in/out so it doesn't click.
    """
    samples = array("h")  # signed 16-bit, matches the mixer format
    for freq, duration_ms in notes:
        count = int(SAMPLE_RATE * duration_ms / 1000)
        attack = int(SAMPLE_RATE * 0.005)    # 5 ms fade in
        release = int(SAMPLE_RATE * 0.020)   # 20 ms fade out
        for i in range(count):
            envelope = 1.0
            if i < attack:
                envelope = i / attack
            elif i > count - release:
                envelope = (count - i) / release
            value = int(32767 * volume * envelope
                        * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
            samples.append(value)  # left channel
            samples.append(value)  # right channel
    return pygame.mixer.Sound(buffer=samples)


def load_sounds():
    """Create the sound effects; returns {} if no audio device is available."""
    if not pygame.mixer.get_init():
        return {}
    try:
        return {
            # Match: happy ascending chime (E5 -> A5).
            "match": make_tone([(659.25, 90), (880.00, 170)]),
            # Mismatch: descending "womp" (G3 -> E3).
            "mismatch": make_tone([(196.00, 120), (164.81, 220)], volume=0.45),
        }
    except pygame.error:
        return {}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main():
    pygame.mixer.pre_init(SAMPLE_RATE, -16, 2, 512)  # 16-bit stereo, small buffer
    pygame.init()
    screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
    pygame.display.set_caption("Memory Match")
    clock = pygame.time.Clock()

    banner_font = pygame.font.SysFont("arial", 64, bold=True)
    hud_font = pygame.font.SysFont("arial", 28, bold=True)
    small_font = pygame.font.SysFont("arial", 24)
    button_font = pygame.font.SysFont("arial", 20, bold=True)
    sounds = load_sounds()

    state = "menu"               # "menu", "playing", "enter_name" or "highscores"
    buttons = menu_buttons()
    hs_button = highscores_button()
    tab_rects = highscore_tab_rects()
    hud_btns = hud_buttons()
    win_btns = win_banner_buttons()
    name_box = name_entry_box()
    name_btns = name_entry_buttons()
    back_btn = highscores_back_button()
    scores, online = await load_scores()
    game = None
    number_font = back_font = None
    player_name = ""
    highscore_tab = 0

    def start_game(index):
        """Create a new round for the chosen difficulty."""
        nonlocal state, game, number_font, back_font
        game = Game(DIFFICULTIES[index], sounds)
        card_size = game.cards[0].rect.width
        number_font, back_font = card_fonts(card_size)
        state = "playing"

    async def save_player_name():
        """Save the entered name to the board, then show the board."""
        nonlocal scores, online, highscore_tab, state
        scores, online = await submit_score(
            scores, online, game.difficulty[0],
            player_name.strip() or "Player",
            game.moves, game.final_time_ms)
        labels = [label for label, _c, _r in DIFFICULTIES]
        highscore_tab = labels.index(game.difficulty[0])
        state = "highscores"

    running = True
    while running:
        # 1. Handle input events.
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            if state == "menu":
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE and not IS_WEB:
                        running = False  # in a browser there is nothing to quit to
                    if event.key == pygame.K_h:
                        state = "highscores"
                    # Keys 1, 2, 3 pick a difficulty (key codes are consecutive).
                    for i in range(len(DIFFICULTIES)):
                        if event.key == pygame.K_1 + i:
                            start_game(i)
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if hs_button.collidepoint(event.pos):
                        state = "highscores"
                    for rect, i in buttons:
                        if rect.collidepoint(event.pos):
                            start_game(i)

            elif state == "playing":
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        state = "menu"
                    if event.key == pygame.K_r:
                        game.reset()
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if game.won:
                        # Win banner buttons (R / Esc work too).
                        for action, rect in win_btns:
                            if rect.collidepoint(event.pos):
                                if action == "again":
                                    game.reset()
                                else:
                                    state = "menu"
                    elif any(rect.collidepoint(event.pos) for _a, rect in hud_btns):
                        # HUD buttons: leave the cards alone when a button is tapped.
                        for action, rect in hud_btns:
                            if rect.collidepoint(event.pos):
                                if action == "restart":
                                    game.reset()
                                else:
                                    state = "menu"
                    else:
                        was_won = game.won
                        game.handle_click(event.pos)
                        # The click that wins the game may earn a high score entry.
                        if (game.won and not was_won
                                and qualifies_for_highscore(scores, game.difficulty[0],
                                                            game.moves, game.final_time_ms)):
                            player_name = ""
                            state = "enter_name"

            elif state == "enter_name":
                if event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        await save_player_name()
                    elif event.key == pygame.K_ESCAPE:
                        state = "playing"  # skip saving, back to the win banner
                    elif event.key == pygame.K_BACKSPACE:
                        player_name = player_name[:-1]
                    elif (len(player_name) < MAX_NAME_LENGTH
                          and event.unicode.isprintable()):
                        player_name += event.unicode
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    for action, rect in name_btns:
                        if rect.collidepoint(event.pos):
                            if action == "save":
                                await save_player_name()
                            else:
                                state = "playing"  # skip saving
                    # No keyboard on touch devices: tapping the input box opens
                    # the browser prompt, which summons the on-screen keyboard.
                    if (state == "enter_name" and IS_WEB
                            and name_box.collidepoint(event.pos)):
                        player_name = prompt_for_name(player_name)

            else:  # highscores
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        state = "menu"
                    for i in range(len(DIFFICULTIES)):
                        if event.key == pygame.K_1 + i:
                            highscore_tab = i
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if back_btn.collidepoint(event.pos):
                        state = "menu"
                    for rect, i in tab_rects:
                        if rect.collidepoint(event.pos):
                            highscore_tab = i

        # 2. Update game state (flip wrong pairs back down).
        if state == "playing":
            game.update()

        # 3. Draw everything.
        screen.fill(BACKGROUND)
        mouse_pos = pygame.mouse.get_pos()
        if state == "menu":
            draw_menu(screen, buttons, hs_button, mouse_pos,
                      banner_font, small_font, hud_font)
        elif state == "highscores":
            draw_highscores(screen, scores, highscore_tab, tab_rects, back_btn,
                            banner_font, hud_font, small_font, button_font, mouse_pos)
        else:  # playing or enter_name
            now = pygame.time.get_ticks()
            for card in game.cards:
                draw_card(screen, card, number_font, back_font, now)
            draw_hud(screen, game, hud_btns, hud_font, button_font, mouse_pos)
            if state == "enter_name":
                draw_name_entry(screen, game, player_name, now, name_box, name_btns,
                                banner_font, hud_font, small_font, button_font, mouse_pos)
            elif game.won:
                draw_win_banner(screen, game, win_btns,
                                banner_font, hud_font, button_font, mouse_pos)
        pygame.display.flip()

        clock.tick(FPS)
        await asyncio.sleep(0)  # let the browser handle the next frame (pygbag)

    pygame.quit()


if __name__ == "__main__":
    asyncio.run(main())
    # Do not add anything below asyncio.run(): on pygbag it is non-blocking
    # and code here would run before the game even starts.
