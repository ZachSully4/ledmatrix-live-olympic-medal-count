"""
Live Olympic Medal Count Plugin for LEDMatrix

Displays a live-updating leaderboard of the 2026 Milano Cortina Winter Olympics
medal count as a continuous horizontal scroll. The header text scrolls first,
followed by each country's name, flag, and gold/silver/bronze counts.

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
from src.common.scroll_helper import ScrollHelper

logger = logging.getLogger(__name__)

# --- Colors ---
COLOR_BLACK = (0, 0, 0)
COLOR_WHITE = (255, 255, 255)
COLOR_LIGHT_BLUE = (135, 206, 250)
COLOR_GOLD = (255, 215, 0)
COLOR_SILVER = (192, 192, 192)
COLOR_BRONZE = (205, 127, 50)
COLOR_DARK_GRAY = (80, 80, 80)

# --- API ---
# ESPN scraping URL for 2026 Winter Olympics (to be implemented)
ESPN_MEDALS_URL = "https://www.espn.com/olympics/winter/2026/medals"
HEADER_TEXT = "MILANO CORTINA 2026"

# Placeholder data for top Winter Olympics countries (until scraping is implemented)
PLACEHOLDER_COUNTRIES = [
    {"id": "NOR", "name": "Norway", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/no.png"},
    {"id": "GER", "name": "Germany", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/de.png"},
    {"id": "USA", "name": "United States", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/us.png"},
    {"id": "CAN", "name": "Canada", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/ca.png"},
    {"id": "SWE", "name": "Sweden", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/se.png"},
    {"id": "SUI", "name": "Switzerland", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/ch.png"},
    {"id": "AUT", "name": "Austria", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/at.png"},
    {"id": "ITA", "name": "Italy", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/it.png"},
    {"id": "FRA", "name": "France", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/fr.png"},
    {"id": "NED", "name": "Netherlands", "gold_medals": 0, "silver_medals": 0, "bronze_medals": 0, "total_medals": 0, "flag_url": "https://flagcdn.com/w80/nl.png"},
]

# Flag dimensions — bigger now that the full display height is available
FLAG_WIDTH = 36
FLAG_HEIGHT = 24


class LiveOlympicMedalCountPlugin(BasePlugin):
    """
    Displays live Olympic medal counts as a continuously scrolling ticker.

    Content order:
        [MILANO CORTINA 2026] → [#1 USA 🇺🇸 G:40 S:44 B:42] → [#2 CHN …] → …

    Supports two view modes via config:
    - top5: Shows the top N countries.
    - usa_only: Shows only USA.
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
        self.scroll_speed: float = float(display_opts.get("scroll_speed", 2.0))
        self.scroll_delay: float = float(display_opts.get("scroll_delay", 0.05))
        self.target_fps: int = int(display_opts.get("target_fps", 120))

        self.update_interval: int = int(data_settings.get("update_interval", 300))
        self.cache_ttl: int = int(data_settings.get("cache_ttl", 300))
        self.top_n: int = int(data_settings.get("top_n_countries", 5))

        # --- Display dimensions ---
        self.width: int = self.display_manager.matrix.width
        self.height: int = self.display_manager.matrix.height

        # --- ScrollHelper (same pattern as live-player-stats) ---
        self.scroll_helper = ScrollHelper(
            self.width,
            self.height,
            logger=self.logger,
        )
        self.scroll_helper.set_frame_based_scrolling(True)
        self.scroll_helper.set_scroll_speed(self.scroll_speed)
        self.scroll_helper.set_scroll_delay(self.scroll_delay)
        self.scroll_helper.set_target_fps(self.target_fps)

        # Enable high FPS scrolling mode (required for 125 FPS display loop)
        self.enable_scrolling = True

        # --- Data state ---
        self.countries: List[Dict[str, Any]] = []
        self.last_fetch_time: float = 0.0
        self.needs_initial_render: bool = True

        # Flag image cache: country_code -> PIL.Image (resized)
        self._flag_cache: Dict[str, Optional[Image.Image]] = {}

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
                self.font_header = ImageFont.truetype(str(press_start), 10)
                self.font_country = ImageFont.truetype(str(press_start), 10)
                self.font_medals = ImageFont.truetype(str(press_start), 10)
            elif small_font.exists():
                self.font_header = ImageFont.truetype(str(small_font), 8)
                self.font_country = ImageFont.truetype(str(small_font), 8)
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
        """Download flags for all countries that aren't cached yet."""
        for country in countries:
            self._get_flag(country)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    def fetch_data(self) -> List[Dict[str, Any]]:
        """
        Return placeholder medal data for 2026 Winter Olympics.

        TODO: Implement ESPN scraping when medals are awarded.
        For now, returns static placeholder data for top Winter Olympics countries.
        """
        # Use placeholder data until ESPN scraping is implemented
        data = PLACEHOLDER_COUNTRIES.copy()

        # Sort: primary by gold_medals desc, secondary by total_medals desc
        data.sort(
            key=lambda c: (c.get("gold_medals", 0), c.get("total_medals", 0)),
            reverse=True,
        )

        # In usa_only mode keep all data so we can find USA regardless of rank
        if self.view_mode == "usa_only":
            top_countries = data
        else:
            top_countries = data[: self.top_n]

        self.logger.info(
            "Using placeholder data — %d countries (ESPN scraping not yet implemented)",
            len(top_countries),
        )
        return top_countries

    # ------------------------------------------------------------------
    # Scrolling content rendering
    # ------------------------------------------------------------------
    def _render_scrolling_content(self) -> None:
        """
        Pre-render the full scrolling ticker image and hand it to ScrollHelper.

        Content items (each a PIL Image, full display height):
            1. Header card — "MILANO CORTINA 2026"
            2. One card per country — name, flag, gold, silver, bronze
        """
        content_items: List[Image.Image] = []

        # --- Header card ---
        content_items.append(self._render_header_card())

        # --- Country cards ---
        countries_to_render = self._get_countries_for_view()

        for rank, country in enumerate(countries_to_render, start=1):
            content_items.append(self._render_country_card(country, rank))

        if not countries_to_render:
            content_items.append(self._render_placeholder("NO DATA"))

        self.scroll_helper.create_scrolling_image(
            content_items=content_items,
            item_gap=24,
            element_gap=8,
        )

        total_w = self.scroll_helper.total_scroll_width
        self.logger.info(
            "Scrolling content created — %d items, total width %dpx",
            len(content_items), total_w,
        )

    def _get_countries_for_view(self) -> List[Dict[str, Any]]:
        """Return the countries list filtered by the current view mode."""
        if self.view_mode == "usa_only":
            for c in self.countries:
                if c.get("id", "").upper() == "USA":
                    return [c]
            return []
        return self.countries[: self.top_n]

    def _render_header_card(self) -> Image.Image:
        """Render the 'MILANO CORTINA 2026' header as a standalone card image."""
        # Measure text
        tmp = Image.new("RGB", (1, 1))
        tmp_draw = ImageDraw.Draw(tmp)
        bbox = tmp_draw.textbbox((0, 0), HEADER_TEXT, font=self.font_header)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        card_w = text_w + 16  # 8px padding each side
        card = Image.new("RGB", (card_w, self.height), COLOR_BLACK)
        draw = ImageDraw.Draw(card)

        # Center vertically
        y = (self.height - text_h) // 2
        draw.text((8, y), HEADER_TEXT, font=self.font_header, fill=COLOR_LIGHT_BLUE)

        return card

    def _render_country_card(self, country: Dict[str, Any], rank: int) -> Image.Image:
        """
        Render a single country card for the scroll.

        Horizontal layout (all vertically centred):
            [#rank CODE]  [FLAG]  [gold-dot NN]  [silver-dot NN]  [bronze-dot NN]
        """
        code = str(country.get("id", "???"))[:3].upper()
        gold = int(country.get("gold_medals", 0))
        silver = int(country.get("silver_medals", 0))
        bronze = int(country.get("bronze_medals", 0))

        # --- Measure widths to calculate card size ---
        tmp = Image.new("RGB", (1, 1))
        tmp_draw = ImageDraw.Draw(tmp)

        rank_code_text = f"#{rank} {code}"
        rc_bbox = tmp_draw.textbbox((0, 0), rank_code_text, font=self.font_country)
        rc_w = rc_bbox[2] - rc_bbox[0]
        rc_h = rc_bbox[3] - rc_bbox[1]

        # Medal sections: each is [dot(10px) + gap(4) + number + gap(8)]
        dot_size = 10
        medal_gap = 8
        medal_section_w = 0
        for count in (gold, silver, bronze):
            count_str = str(count)
            cb = tmp_draw.textbbox((0, 0), count_str, font=self.font_medals)
            medal_section_w += dot_size + 4 + (cb[2] - cb[0]) + medal_gap

        gap_after_text = 6
        gap_after_flag = 8

        card_w = 8 + rc_w + gap_after_text + FLAG_WIDTH + gap_after_flag + medal_section_w + 8
        card = Image.new("RGB", (card_w, self.height), COLOR_BLACK)
        draw = ImageDraw.Draw(card)

        x = 8  # left padding
        mid_y = self.height // 2

        # --- Rank + Country Code ---
        text_y = mid_y - rc_h // 2
        draw.text((x, text_y), rank_code_text, font=self.font_country, fill=COLOR_WHITE)
        x += rc_w + gap_after_text

        # --- Flag ---
        flag_img = self._get_flag(country)
        if flag_img is not None:
            flag_y = mid_y - FLAG_HEIGHT // 2
            card.paste(flag_img, (x, flag_y))
        x += FLAG_WIDTH + gap_after_flag

        # --- Medal counts ---
        medals_bbox = tmp_draw.textbbox((0, 0), "0", font=self.font_medals)
        medal_text_h = medals_bbox[3] - medals_bbox[1]

        for count, color in [(gold, COLOR_GOLD), (silver, COLOR_SILVER), (bronze, COLOR_BRONZE)]:
            # Draw filled circle
            dot_y = mid_y - dot_size // 2
            draw.ellipse(
                [x, dot_y, x + dot_size, dot_y + dot_size],
                fill=color,
            )
            x += dot_size + 4

            # Draw count number
            count_str = str(count)
            count_y = mid_y - medal_text_h // 2
            draw.text((x, count_y), count_str, font=self.font_medals, fill=color)
            cb = draw.textbbox((0, 0), count_str, font=self.font_medals)
            x += (cb[2] - cb[0]) + medal_gap

        return card

    def _render_placeholder(self, message: str) -> Image.Image:
        """Render a placeholder card for when there's no data."""
        tmp = Image.new("RGB", (1, 1))
        tmp_draw = ImageDraw.Draw(tmp)
        bbox = tmp_draw.textbbox((0, 0), message, font=self.font_country)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        card_w = text_w + 16
        card = Image.new("RGB", (card_w, self.height), COLOR_BLACK)
        draw = ImageDraw.Draw(card)
        draw.text((8, (self.height - text_h) // 2), message, font=self.font_country, fill=COLOR_WHITE)
        return card

    # ------------------------------------------------------------------
    # BasePlugin interface
    # ------------------------------------------------------------------
    def update(self) -> None:
        """Fetch fresh data from the API on the configured interval."""
        now = time.time()
        if now - self.last_fetch_time >= self.update_interval or not self.countries:
            self.countries = self.fetch_data()
            self.last_fetch_time = now
            self._prefetch_flags(self.countries)
            self._render_scrolling_content()
            self.needs_initial_render = False

    def display(self, force_clear: bool = False) -> None:
        """
        Advance the scroll and push the visible frame to the matrix.

        Matches the live-player-stats scroll loop: detects wrap-around
        and resets distance tracking so the scroll loops indefinitely.
        """
        try:
            if force_clear:
                self.display_manager.clear()

            # Ensure we have a scrolling image
            if self.needs_initial_render:
                return

            # Record position before update for wrap detection
            old_pos = self.scroll_helper.scroll_position

            # Advance scroll
            self.scroll_helper.update_scroll_position()

            # Detect wrap-around (position jumped backward significantly)
            new_pos = self.scroll_helper.scroll_position
            wrapped = (old_pos - new_pos) > self.scroll_helper.display_width

            if wrapped:
                self.logger.info(
                    "Scroll wrap detected (%.0f -> %.0f)", old_pos, new_pos
                )
                # Reset distance tracking to keep the scroll looping indefinitely
                self.scroll_helper.scroll_complete = False
                self.scroll_helper.total_distance_scrolled = 0.0

            # Get the visible window
            visible = self.scroll_helper.get_visible_portion()
            if visible is None:
                return

            # Push to display
            if (
                not hasattr(self.display_manager, "image")
                or self.display_manager.image is None
                or self.display_manager.image.size != (self.width, self.height)
            ):
                self.display_manager.image = Image.new(
                    "RGB", (self.width, self.height), COLOR_BLACK
                )

            if visible.size == (self.width, self.height):
                self.display_manager.image.paste(visible, (0, 0))
            else:
                visible = visible.resize(
                    (self.width, self.height), Image.Resampling.LANCZOS
                )
                self.display_manager.image.paste(visible, (0, 0))

            self.display_manager.update_display()

        except Exception as e:
            self.logger.error("Error displaying medal count: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # Duration / cycle support
    # ------------------------------------------------------------------
    def supports_dynamic_duration(self) -> bool:
        return True

    def is_cycle_complete(self) -> bool:
        return False  # continuous scroll — managed internally

    def get_display_duration(self) -> float:
        return self.scroll_helper.get_dynamic_duration()

    def reset_cycle_state(self) -> None:
        self.scroll_helper.reset_scroll()

    # ------------------------------------------------------------------
    # Config change
    # ------------------------------------------------------------------
    def on_config_change(self, new_config: Dict[str, Any]) -> None:
        super().on_config_change(new_config)

        display_opts = self.config.get("display_options", {})
        data_settings = self.config.get("data_settings", {})

        self.view_mode = display_opts.get("view_mode", "top5")
        self.scroll_speed = float(display_opts.get("scroll_speed", 2.0))
        self.scroll_delay = float(display_opts.get("scroll_delay", 0.05))
        self.target_fps = int(display_opts.get("target_fps", 120))

        self.scroll_helper.set_scroll_speed(self.scroll_speed)
        self.scroll_helper.set_scroll_delay(self.scroll_delay)
        self.scroll_helper.set_target_fps(self.target_fps)

        self.update_interval = int(data_settings.get("update_interval", 300))
        self.cache_ttl = int(data_settings.get("cache_ttl", 300))
        self.top_n = int(data_settings.get("top_n_countries", 5))

        # Force fresh fetch + re-render
        self.last_fetch_time = 0.0
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
            "last_fetch_time": self.last_fetch_time,
            "scroll_info": self.scroll_helper.get_scroll_info(),
        })
        return info

    def cleanup(self) -> None:
        self.logger.info("Cleaning up Olympic Medal Count plugin")
        self.scroll_helper.clear_cache()
        super().cleanup()
