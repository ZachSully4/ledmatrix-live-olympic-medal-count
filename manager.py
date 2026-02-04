"""
Live Olympic Medal Count Plugin for LEDMatrix

Displays a live-updating leaderboard of the 2026 Milano Cortina Winter Olympics
medal count. Cycles through the top countries showing gold, silver, and bronze
totals with color-coded medal indicators.

API: https://apis.codante.io/olympic-games/countries
API Version: 1.0.0
"""

import io
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from PIL import Image, ImageDraw, ImageFont

from src.plugin_system.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

# --- Colors ---
COLOR_BLACK = (0, 0, 0)
COLOR_WHITE = (255, 255, 255)
COLOR_LIGHT_BLUE = (135, 206, 250)
COLOR_GOLD = (255, 215, 0)
COLOR_SILVER = (192, 192, 192)
COLOR_BRONZE = (205, 127, 50)
COLOR_DARK_GRAY = (60, 60, 60)

# --- API ---
API_URL = "https://apis.codante.io/olympic-games/countries"
HEADER_TEXT = "MILANO CORTINA 2026"

# Flag dimensions for LED matrix (width x height in pixels)
FLAG_WIDTH = 12
FLAG_HEIGHT = 8


class LiveOlympicMedalCountPlugin(BasePlugin):
    """
    Displays live Olympic medal counts on the LED matrix.

    Supports two view modes:
    - top5: Cycles through the top N countries by medal count.
    - usa_only: Shows only USA medal totals.
    """

    def __init__(
        self,
        plugin_id: str,
        config: Dict[str, Any],
        display_manager,
        cache_manager,
        plugin_manager,
    ):
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)

        # --- Config ---
        display_opts = self.config.get("display_options", {})
        data_settings = self.config.get("data_settings", {})

        self.view_mode: str = display_opts.get("view_mode", "top5")
        self.cycle_interval: float = float(display_opts.get("cycle_interval", 10))
        self.header_scroll_speed: float = float(display_opts.get("header_scroll_speed", 1.0))
        self.target_fps: int = int(display_opts.get("target_fps", 30))

        self.update_interval: int = int(data_settings.get("update_interval", 300))
        self.cache_ttl: int = int(data_settings.get("cache_ttl", 300))
        self.top_n: int = int(data_settings.get("top_n_countries", 5))

        # --- Display state ---
        self.width: int = self.display_manager.matrix.width
        self.height: int = self.display_manager.matrix.height

        # Data cache
        self.countries: List[Dict[str, Any]] = []
        self.last_fetch_time: float = 0.0

        # Cycling state
        self.current_country_index: int = 0
        self.last_cycle_time: float = time.time()
        self.cycle_complete: bool = False

        # Header scroll state
        self.header_scroll_x: float = 0.0

        # Flag image cache: country_code -> PIL.Image (resized)
        self._flag_cache: Dict[str, Optional[Image.Image]] = {}

        # Frame timing
        self.last_frame_time: float = time.time()
        self.frame_interval: float = 1.0 / self.target_fps

        # --- Fonts ---
        self.font_header = None
        self.font_country = None
        self.font_medals = None
        self._load_fonts()

        self.logger.info(
            "Olympic Medal Count plugin initialized — mode=%s, top_n=%d, update_interval=%ds",
            self.view_mode, self.top_n, self.update_interval,
        )

    # ------------------------------------------------------------------
    # Font loading
    # ------------------------------------------------------------------
    def _load_fonts(self) -> None:
        """Load fonts from the project assets directory."""
        project_root = Path(__file__).resolve().parent.parent.parent
        fonts_dir = project_root / "assets" / "fonts"

        press_start = fonts_dir / "PressStart2P-Regular.ttf"
        small_font = fonts_dir / "4x6-font.ttf"

        try:
            if press_start.exists():
                self.font_header = ImageFont.truetype(str(press_start), 8)
                self.font_country = ImageFont.truetype(str(press_start), 8)
                self.font_medals = ImageFont.truetype(str(press_start), 8)
            elif small_font.exists():
                self.font_header = ImageFont.truetype(str(small_font), 6)
                self.font_country = ImageFont.truetype(str(small_font), 6)
                self.font_medals = ImageFont.truetype(str(small_font), 6)
            else:
                self.font_header = ImageFont.load_default()
                self.font_country = ImageFont.load_default()
                self.font_medals = ImageFont.load_default()
        except Exception as exc:
            self.logger.warning("Could not load custom fonts: %s — using defaults", exc)
            self.font_header = ImageFont.load_default()
            self.font_country = ImageFont.load_default()
            self.font_medals = ImageFont.load_default()

    # ------------------------------------------------------------------
    # Flag loading
    # ------------------------------------------------------------------
    def _get_flag(self, country: Dict[str, Any]) -> Optional[Image.Image]:
        """
        Get a country's flag image, downloading and caching as needed.

        Uses the ``flag_url`` field returned by the API. Images are
        downloaded once, resized to FLAG_WIDTH x FLAG_HEIGHT, converted
        to RGB, and kept in an in-memory dict for the lifetime of the
        plugin.
        """
        code = str(country.get("id", "")).upper()
        if not code:
            return None

        # Return from memory cache if available
        if code in self._flag_cache:
            return self._flag_cache[code]

        flag_url = country.get("flag_url")
        if not flag_url:
            self._flag_cache[code] = None
            return None

        try:
            resp = requests.get(flag_url, timeout=10)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content))
            img = img.convert("RGB")
            img = img.resize((FLAG_WIDTH, FLAG_HEIGHT), Image.LANCZOS)
            self._flag_cache[code] = img
            self.logger.debug("Cached flag for %s (%dx%d)", code, FLAG_WIDTH, FLAG_HEIGHT)
            return img
        except Exception as exc:
            self.logger.warning("Could not fetch flag for %s: %s", code, exc)
            self._flag_cache[code] = None
            return None

    def _prefetch_flags(self, countries: List[Dict[str, Any]]) -> None:
        """Download flags for all countries in the list that aren't cached yet."""
        for country in countries:
            self._get_flag(country)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    def fetch_data(self) -> List[Dict[str, Any]]:
        """
        Fetch medal data from the Olympic Games API.

        Returns a sorted list of the top N countries. Falls back to cached
        data when the API is unreachable.
        """
        cache_key = f"{self.plugin_id}_medal_data"

        # Check local cache first
        if self.cache_manager:
            cached = self.cache_manager.get(cache_key, max_age=self.cache_ttl)
            if cached:
                self.logger.debug("Using cached medal data (%d countries)", len(cached))
                return cached

        try:
            response = requests.get(API_URL, timeout=15)
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", [])

            # Sort: primary by gold_medals desc, secondary by total_medals desc
            data.sort(
                key=lambda c: (c.get("gold_medals", 0), c.get("total_medals", 0)),
                reverse=True,
            )

            top_countries = data[: self.top_n]

            # Persist to cache
            if self.cache_manager:
                self.cache_manager.set(cache_key, top_countries)

            self.logger.info(
                "Fetched medal data — top country: %s (%d gold)",
                top_countries[0].get("id", "???") if top_countries else "N/A",
                top_countries[0].get("gold_medals", 0) if top_countries else 0,
            )
            return top_countries

        except requests.exceptions.Timeout:
            self.logger.warning("API request timed out — using stale cache if available")
        except requests.exceptions.ConnectionError:
            self.logger.warning("API connection error — using stale cache if available")
        except requests.exceptions.HTTPError as exc:
            self.logger.warning("API HTTP error %s — using stale cache if available", exc)
        except Exception as exc:
            self.logger.error("Unexpected error fetching medal data: %s", exc, exc_info=True)

        # Fallback: return whatever is in cache (even if expired) or empty list
        if self.cache_manager:
            stale = self.cache_manager.get(cache_key)
            if stale:
                self.logger.info("Returning stale cached data (%d countries)", len(stale))
                return stale

        return []

    # ------------------------------------------------------------------
    # BasePlugin interface
    # ------------------------------------------------------------------
    def update(self) -> None:
        """Fetch fresh data from the API on the configured interval."""
        now = time.time()
        if now - self.last_fetch_time >= self.update_interval or not self.countries:
            self.countries = self.fetch_data()
            self.last_fetch_time = now
            # Pre-download flags so display() never blocks on network I/O
            self._prefetch_flags(self.countries)

    def display(self, force_clear: bool = False) -> None:
        """Render the current frame to the LED matrix."""
        now = time.time()

        # Throttle to target FPS
        elapsed = now - self.last_frame_time
        if elapsed < self.frame_interval:
            return
        self.last_frame_time = now

        if force_clear:
            self.display_manager.clear()

        # Build frame
        img = Image.new("RGB", (self.width, self.height), COLOR_BLACK)
        draw = ImageDraw.Draw(img)

        # --- Header ---
        header_height = self._draw_header(draw, img)

        # --- Body ---
        if self.view_mode == "usa_only":
            self._draw_usa_only(draw, img, header_height)
        else:
            self._draw_top_n(draw, img, header_height, now)

        # Push to display
        self.display_manager.image = img
        self.display_manager.update_display()

    # ------------------------------------------------------------------
    # Drawing helpers
    # ------------------------------------------------------------------
    def _draw_header(self, draw: ImageDraw.ImageDraw, img: Image.Image) -> int:
        """
        Draw the scrolling header text. Returns the height consumed.
        """
        header_y = 1
        bbox = draw.textbbox((0, 0), HEADER_TEXT, font=self.font_header)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        header_height = text_h + 4  # 2px padding top/bottom

        if text_w <= self.width:
            # Text fits — center it
            x = (self.width - text_w) // 2
            draw.text((x, header_y), HEADER_TEXT, font=self.font_header, fill=COLOR_LIGHT_BLUE)
        else:
            # Scroll the header
            draw.text((int(-self.header_scroll_x), header_y), HEADER_TEXT,
                       font=self.font_header, fill=COLOR_LIGHT_BLUE)
            # Wrap-around copy
            wrap_x = int(-self.header_scroll_x) + text_w + self.width // 2
            draw.text((wrap_x, header_y), HEADER_TEXT,
                       font=self.font_header, fill=COLOR_LIGHT_BLUE)

            self.header_scroll_x += self.header_scroll_speed
            total_scroll = text_w + self.width // 2
            if self.header_scroll_x >= total_scroll:
                self.header_scroll_x = 0.0

        # Draw a thin separator line below the header
        sep_y = header_y + header_height
        draw.line([(0, sep_y), (self.width - 1, sep_y)], fill=COLOR_DARK_GRAY)

        return sep_y + 2  # return y-offset for body content

    def _draw_top_n(self, draw: ImageDraw.ImageDraw, img: Image.Image, y_start: int, now: float) -> None:
        """Draw the current country in the Top-N rotation."""
        if not self.countries:
            self._draw_no_data(draw, y_start)
            return

        # Cycle to next country
        if now - self.last_cycle_time >= self.cycle_interval:
            self.current_country_index += 1
            if self.current_country_index >= len(self.countries):
                self.current_country_index = 0
                self.cycle_complete = True
            self.last_cycle_time = now

        country = self.countries[self.current_country_index]
        rank = self.current_country_index + 1
        self._draw_country_card(draw, img, country, rank, y_start)

    def _draw_usa_only(self, draw: ImageDraw.ImageDraw, img: Image.Image, y_start: int) -> None:
        """Draw only the USA entry."""
        usa = None
        for i, c in enumerate(self.countries):
            if c.get("id", "").upper() == "USA":
                usa = c
                break

        if usa is None:
            # USA not in top-N — do a direct lookup from full cached data
            cache_key = f"{self.plugin_id}_medal_data"
            all_data = self.cache_manager.get(cache_key) if self.cache_manager else None
            if all_data:
                for c in all_data:
                    if c.get("id", "").upper() == "USA":
                        usa = c
                        break

        if usa is None:
            self._draw_no_data(draw, y_start, "USA DATA N/A")
            return

        self._draw_country_card(draw, img, usa, usa.get("rank", "?"), y_start)

    def _draw_country_card(
        self,
        draw: ImageDraw.ImageDraw,
        img: Image.Image,
        country: Dict[str, Any],
        rank: Any,
        y_start: int,
    ) -> None:
        """
        Render a single country's medal card.

        Layout (within available body area):
            Line 1: [flag] #rank  COUNTRY_CODE
            Line 2: [gold dot] NN  [silver dot] NN  [bronze dot] NN
            Line 3: Total: NN
        """
        code = str(country.get("id", "???"))[:3].upper()
        gold = int(country.get("gold_medals", 0))
        silver = int(country.get("silver_medals", 0))
        bronze = int(country.get("bronze_medals", 0))
        total = int(country.get("total_medals", 0))

        body_height = self.height - y_start
        line_h = max(body_height // 3, 8)
        y = y_start
        x_cursor = 2

        # --- Line 1: Flag + Rank + Country Code ---
        flag_img = self._get_flag(country)
        if flag_img is not None:
            img.paste(flag_img, (x_cursor, y))
            x_cursor += FLAG_WIDTH + 2

        rank_text = f"#{rank}"
        draw.text((x_cursor, y), rank_text, font=self.font_medals, fill=COLOR_DARK_GRAY)
        rank_bbox = draw.textbbox((0, 0), rank_text, font=self.font_medals)
        rank_w = rank_bbox[2] - rank_bbox[0]

        code_x = x_cursor + rank_w + 4
        draw.text((code_x, y), code, font=self.font_country, fill=COLOR_WHITE)
        y += line_h

        # --- Line 2: Medal counts with colored dots ---
        dot_radius = 2
        medal_x = 2

        for medal_count, color in [(gold, COLOR_GOLD), (silver, COLOR_SILVER), (bronze, COLOR_BRONZE)]:
            # Draw small colored dot
            dot_y = y + line_h // 2
            draw.ellipse(
                [medal_x, dot_y - dot_radius, medal_x + dot_radius * 2, dot_y + dot_radius],
                fill=color,
            )
            medal_x += dot_radius * 2 + 2

            # Draw count
            count_str = str(medal_count)
            draw.text((medal_x, y), count_str, font=self.font_medals, fill=color)
            count_bbox = draw.textbbox((0, 0), count_str, font=self.font_medals)
            count_w = count_bbox[2] - count_bbox[0]
            medal_x += count_w + 4

        y += line_h

        # --- Line 3: Total (if space permits) ---
        if y + 6 <= self.height:
            total_text = f"TOT:{total}"
            draw.text((2, y), total_text, font=self.font_medals, fill=COLOR_WHITE)

    def _draw_no_data(self, draw: ImageDraw.ImageDraw, y_start: int, message: str = "NO DATA") -> None:
        """Draw a fallback message when data is unavailable."""
        bbox = draw.textbbox((0, 0), message, font=self.font_country)
        text_w = bbox[2] - bbox[0]
        x = (self.width - text_w) // 2
        y = y_start + (self.height - y_start) // 3
        draw.text((x, y), message, font=self.font_country, fill=COLOR_WHITE)

    # ------------------------------------------------------------------
    # Cycle / duration support
    # ------------------------------------------------------------------
    def get_display_duration(self) -> float:
        """Total duration = cycle_interval * number of countries."""
        if self.view_mode == "usa_only":
            return self.config.get("display_duration", 15.0)
        count = max(len(self.countries), 1)
        return self.cycle_interval * count

    def supports_dynamic_duration(self) -> bool:
        return self.view_mode == "top5"

    def is_cycle_complete(self) -> bool:
        if self.view_mode == "usa_only":
            return True
        return self.cycle_complete

    def reset_cycle_state(self) -> None:
        self.current_country_index = 0
        self.last_cycle_time = time.time()
        self.cycle_complete = False

    # ------------------------------------------------------------------
    # Config change
    # ------------------------------------------------------------------
    def on_config_change(self, new_config: Dict[str, Any]) -> None:
        super().on_config_change(new_config)

        display_opts = self.config.get("display_options", {})
        data_settings = self.config.get("data_settings", {})

        self.view_mode = display_opts.get("view_mode", "top5")
        self.cycle_interval = float(display_opts.get("cycle_interval", 10))
        self.header_scroll_speed = float(display_opts.get("header_scroll_speed", 1.0))
        self.target_fps = int(display_opts.get("target_fps", 30))
        self.frame_interval = 1.0 / self.target_fps

        self.update_interval = int(data_settings.get("update_interval", 300))
        self.cache_ttl = int(data_settings.get("cache_ttl", 300))
        self.top_n = int(data_settings.get("top_n_countries", 5))

        # Force a fresh fetch on next update
        self.last_fetch_time = 0.0
        self.reset_cycle_state()
        self.logger.info("Config updated — mode=%s, top_n=%d", self.view_mode, self.top_n)

    def validate_config(self) -> bool:
        if not super().validate_config():
            return False

        display_opts = self.config.get("display_options", {})
        view_mode = display_opts.get("view_mode", "top5")
        if view_mode not in ("top5", "usa_only"):
            self.logger.error("Invalid view_mode: %s (must be 'top5' or 'usa_only')", view_mode)
            return False

        return True

    def get_info(self) -> Dict[str, Any]:
        info = super().get_info()
        info.update({
            "view_mode": self.view_mode,
            "countries_loaded": len(self.countries),
            "current_country_index": self.current_country_index,
            "last_fetch_time": self.last_fetch_time,
        })
        return info

    def cleanup(self) -> None:
        self.logger.info("Cleaning up Olympic Medal Count plugin")
        super().cleanup()
