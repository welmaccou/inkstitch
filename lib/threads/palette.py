# Authors: see git history
#
# Copyright (c) 2010 Authors
# Licensed under the GNU GPL version 3.0 or later.  See the file LICENSE for details.

from collections.abc import Set
from collections import OrderedDict

from colormath2.color_conversions import convert_color
from colormath2.color_diff import delta_e_cie1994
from colormath2.color_objects import LabColor, sRGBColor

from .color import ThreadColor


def compare_thread_colors(color1, color2):
    # K_L=2 indicates textiles
    return delta_e_cie1994(color1, color2, K_L=2)


class ThreadPalette(Set):
    """Holds a set of ThreadColors all from the same manufacturer."""

    MAX_NEAREST_CACHE_ITEMS = 2048
    MAX_GLOBAL_LAB_CACHE_ITEMS = 4096
    _global_lab_cache = OrderedDict()

    def __init__(self, palette_file):
        self.threads = dict()
        self._nearest_cache = OrderedDict()
        self.parse_palette_file(palette_file)

    @classmethod
    def _get_or_build_lab(cls, rgb):
        key = tuple(int(channel) for channel in rgb)
        cached = cls._global_lab_cache.get(key)
        if cached is not None:
            cls._global_lab_cache.move_to_end(key)
            return cached

        lab_value = convert_color(sRGBColor(*key, is_upscaled=True), LabColor)
        cls._global_lab_cache[key] = lab_value
        cls._global_lab_cache.move_to_end(key)
        while len(cls._global_lab_cache) > cls.MAX_GLOBAL_LAB_CACHE_ITEMS:
            cls._global_lab_cache.popitem(last=False)
        return lab_value

    def parse_palette_file(self, palette_file):
        """Read a GIMP palette file and load thread colors.

        Example file:

        GIMP Palette
        Name: Ink/Stitch: Metro
        Columns: 4
        # RGB Value                                 Color Name Number
        240     186     212                         Sugar Pink   1624
        237     171     194                           Carnatio   1636

        """

        with open(palette_file, encoding='utf8') as palette:
            try:
                line = palette.readline().strip()
            except UnicodeDecodeError:
                # File has wrong encoding. Can't read this file
                self.is_gimp_palette = False
                return

            self.is_gimp_palette = True
            if line.lower() != "gimp palette":
                self.is_gimp_palette = False
                return

            self.name = palette.readline().strip()
            if self.name.lower().startswith('name: ink/stitch: '):
                self.name = self.name[18:]

            # number of columns
            palette.readline()

            # headers
            palette.readline()

            for line in palette:
                try:
                    fields = line.split(None, 3)
                    thread_color = [int(field) for field in fields[:3]]
                    thread_name, thread_number = fields[3].strip().rsplit(" ", 1)
                    thread_name = thread_name.strip()

                    thread = ThreadColor(thread_color, thread_name, thread_number, manufacturer=self.name, description=thread_name)
                    self.threads[thread] = convert_color(sRGBColor(*thread_color, is_upscaled=True), LabColor)
                except (ValueError, IndexError):
                    continue

    def __contains__(self, thread):
        return thread in self.threads

    def __iter__(self):
        return iter(self.threads)

    def __len__(self):
        return len(self.threads)

    def nearest_color(self, color):
        """Find the thread in this palette that looks the most like the specified color."""

        if isinstance(color, ThreadColor):
            color = color.rgb

        color_key = tuple(int(channel) for channel in color)
        cached = self._nearest_cache.get(color_key)
        if cached is not None:
            self._nearest_cache.move_to_end(color_key)
            return cached

        color = self._get_or_build_lab(color)
        nearest = min(self, key=lambda thread: compare_thread_colors(self.threads[thread], color))
        self._nearest_cache[color_key] = nearest
        self._nearest_cache.move_to_end(color_key)
        while len(self._nearest_cache) > self.MAX_NEAREST_CACHE_ITEMS:
            self._nearest_cache.popitem(last=False)
        return nearest
