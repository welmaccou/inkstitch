# Authors: ver histórico do git
# Modificado para Modo Daemon
# Copyright (c) 2025 Authors
# Licensed under the GNU GPL version 3.0 or later.  See the file LICENSE for details.

import json
import os
import string
import sys
import tempfile
import base64
import io
import time
import threading
from collections import OrderedDict
from copy import deepcopy 
from zipfile import ZipFile

from inkex import Boolean, Group
from lxml import etree

import pystitch

from ..extensions.lettering_along_path import TextAlongPath
from ..lettering import get_font_by_name
from ..output import write_embroidery_file
from ..stitch_plan import stitch_groups_to_stitch_plan
from ..svg import PIXELS_PER_MM, get_correction_transform
from ..threads import ThreadCatalog
from ..utils import DotDict
from .base import InkstitchExtension

class DaemonLettering(InkstitchExtension):
    MAX_RESPONSE_CACHE_ITEMS = 32

    def __init__(self, *args, **kwargs):
        InkstitchExtension.__init__(self)

        self._font_cache = {}
        self._response_cache = OrderedDict()
        self._stdout_lock = threading.Lock()

        self.arg_parser.add_argument('--notebook')
        self.arg_parser.add_argument('--text', type=str, default='', dest='text')
        self.arg_parser.add_argument('--separator', type=str, default='', dest='separator')
        self.arg_parser.add_argument('--font', type=str, default='', dest='font')
        self.arg_parser.add_argument('--scale', type=int, default=100, dest='scale')
        self.arg_parser.add_argument('--color-sort', type=str, default='off', dest='color_sort')
        self.arg_parser.add_argument('--trim', type=str, default='off', dest='trim')
        self.arg_parser.add_argument('--use-command-symbols', type=Boolean, default=False, dest='command_symbols')
        self.arg_parser.add_argument('--text-align', type=str, default='left', dest='text_align')
        self.arg_parser.add_argument('--letter_spacing', type=float, default=0, dest='letter_spacing')
        self.arg_parser.add_argument('--word_spacing', type=float, default=0, dest='word_spacing')
        self.arg_parser.add_argument('--line_height', type=float, default=0, dest='line_height')
        self.arg_parser.add_argument('--text-position', type=str, default='left', dest='text_position')
        self.arg_parser.add_argument('--file-formats', type=str, default='', dest='formats')

    def effect(self):
        pass

    def _emit_json(self, payload):
        with self._stdout_lock:
            print(json.dumps(payload, ensure_ascii=False), flush=True)

    def run(self, args=None):
        from io import BytesIO
        import inkex
        from ..metadata import InkStitchMetadata
        
        svg_str = b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd" width="100mm" height="100mm" viewBox="0 0 100 100"><sodipodi:namedview id="namedview1"/><metadata id="metadata1"><inkstitch_svg_version>3</inkstitch_svg_version></metadata></svg>'
        self.document = inkex.load_svg(BytesIO(svg_str))
        self.svg = self.document.getroot()
        
        # Envia sinal de pronto
        self._emit_json({"status": "ready"})

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            
            try:
                payload = json.loads(line)
                action = payload.get("action")
                if action == "stop":
                    break
                elif action == "generate":
                    self.handle_generate(payload)
                else:
                    self.send_error(f"Unknown action: {action}")
            except Exception as e:
                self.send_error(f"Error parsing or handling: {str(e)}")

    def send_error(self, message, request_id=None):
        payload = {"status": "error", "message": message}
        if request_id:
            payload["request_id"] = request_id
        self._emit_json(payload)

    def _emit_progress(self, request_id, message, progress_pct=None):
        payload = {
            "status": "progress",
            "stage": "generate_points",
            "message": message,
        }
        if request_id:
            payload["request_id"] = request_id
        if progress_pct is not None:
            payload["progress_pct"] = int(max(0, min(100, progress_pct)))
        self._emit_json(payload)

    def _should_skip_stitch_plan_computation(self, file_formats):
        include_preview = getattr(self, "include_preview", False)
        return self.draft_mode and (not include_preview) and len(file_formats) == 1 and file_formats[0] == 'svg'

    def _render_text_svg_only(self, text, text_positioning_path):
        self.settings = DotDict({
            "text": text,
            "text_align": self.text_align,
            "back_and_forth": not self.draft_mode,
            "font": self.font.marked_custom_font_id,
            "scale": int(self.scale * 100),
            "trim_option": self.trim,
            "use_trim_symbols": self.options.command_symbols,
            "color_sort": self.color_sort,
            "letter_spacing": self.options.letter_spacing,
            "word_spacing": self.options.word_spacing,
            "line_height": self.options.line_height
        })

        lettering_group = Group()
        lettering_group.label = "Ink/Stitch Lettering"
        lettering_group.set('inkstitch:lettering', json.dumps(self.settings))
        self.svg.append(lettering_group)
        lettering_group.set("transform", get_correction_transform(lettering_group, child=True))

        destination_group = Group()
        destination_group.label = f"{self.font.name} scale {self.scale * 100}%"
        lettering_group.append(destination_group)

        self.font.render_text(
            text,
            destination_group,
            trim_option=self.trim,
            use_trim_symbols=self.options.command_symbols,
            color_sort=self.color_sort,
            text_align=self.text_align,
            letter_spacing=self.options.letter_spacing,
            word_spacing=self.options.word_spacing,
            line_height=self.options.line_height
        )

        destination_group.attrib['transform'] = f'scale({self.scale})'

        if text_positioning_path is not None:
            parent = text_positioning_path.getparent()
            index = parent.index(text_positioning_path)
            parent.insert(index, lettering_group)
            TextAlongPath(self.svg, lettering_group, text_positioning_path, self.options.text_position)
            text_positioning_path.delete()

        # Keep element state refreshed between iterations for consistent cleanup behavior.
        self.get_elements()

        return lettering_group

    def _get_cached_font(self, font_id):
        cached = self._font_cache.get(font_id)
        if cached is not None:
            return cached

        font = get_font_by_name(font_id, False)
        if font is not None:
            self._font_cache[font_id] = font
        return font

    def _build_cache_key(self, payload, file_formats):
        return json.dumps(
            {
                "text": payload.get("text", ""),
                "separator": payload.get("separator", "\n"),
                "font": payload.get("font", ""),
                "scale": int(payload.get("scale", 100)),
                "trim": payload.get("trim", "off"),
                "text_align": payload.get("text_align", "left"),
                "color_sort": payload.get("color_sort", "off"),
                "command_symbols": bool(payload.get("command_symbols", False)),
                "letter_spacing": float(payload.get("letter_spacing", 0.0)),
                "word_spacing": float(payload.get("word_spacing", 0.0)),
                "line_height": float(payload.get("line_height", 0.0)),
                "text_position": payload.get("text_position", "left"),
                "draft_mode": bool(payload.get("draft_mode", False)),
                "include_preview": bool(payload.get("include_preview", False)),
                "formats": list(file_formats),
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    def _get_cached_response(self, cache_key):
        cached = self._response_cache.get(cache_key)
        if cached is None:
            return None

        # LRU bump
        self._response_cache.move_to_end(cache_key)
        return cached

    def _set_cached_response(self, cache_key, response_payload):
        self._response_cache[cache_key] = response_payload
        self._response_cache.move_to_end(cache_key)

        while len(self._response_cache) > self.MAX_RESPONSE_CACHE_ITEMS:
            self._response_cache.popitem(last=False)

    @staticmethod
    def _preview_command_for_stitch(stitch):
        if stitch.color_change or stitch.stop:
            return 2
        if stitch.trim:
            return 3
        if stitch.jump:
            return 1
        return 0

    @staticmethod
    def _preview_coord(value):
        return round((float(value) / PIXELS_PER_MM) * 10.0, 3)

    def _serialize_preview_payload(self, stitch_plan):
        bounds = stitch_plan.bounding_box
        colors = []
        blocks = []

        for block_index, color_block in enumerate(stitch_plan):
            thread_color = color_block.color.visible_on_white if color_block.color else None
            colors.append(
                {
                    "hex": thread_color.to_hex_str() if thread_color else "#000000",
                    "name": getattr(thread_color, "description", "") or getattr(thread_color, "name", "") or "",
                }
            )

            block_stitches = []
            stitch_count = 0
            for stitch in color_block:
                cmd = self._preview_command_for_stitch(stitch)
                block_stitches.append([
                    self._preview_coord(stitch.x),
                    self._preview_coord(stitch.y),
                    cmd,
                ])
                if cmd == 0:
                    stitch_count += 1

            if stitch_count == 0:
                continue

            blocks.append({
                "color_index": block_index,
                "stitches": block_stitches,
            })

        return {
            "bounds": [self._preview_coord(value) for value in bounds],
            "colors": colors,
            "blocks": blocks,
            "stitches": stitch_plan.num_stitches,
            "validation_error": False,
            "validation_reason": "",
            "source_format": "daemon_preview",
        }

    def handle_generate(self, payload):
        started_at = time.perf_counter()
        stitch_time_total = 0.0
        package_started_at = None
        heartbeat_stop = None
        heartbeat_thread = None
        request_id = payload.get("request_id")
        text_input = payload.get("text", "")
        if not text_input:
            self.send_error("Please specify a text", request_id=request_id)
            return
            
        font_id = payload.get("font", "")
        if not font_id:
            self.send_error("Please specify a font", request_id=request_id)
            return
            
        self.font = self._get_cached_font(font_id)
        if self.font is None:
            self.send_error("Please specify a valid font name.", request_id=request_id)
            return

        file_formats = payload.get("formats", ["svg", "pes"])
        available_formats = [file_format['extension'] for file_format in pystitch.supported_formats()] + ['svg']
        file_formats = [f.strip().lower() for f in file_formats if f.strip().lower() in available_formats]
        if not file_formats:
            self.send_error("No valid formats", request_id=request_id)
            return

        cache_key = self._build_cache_key(payload, file_formats)
        cached_response = self._get_cached_response(cache_key)
        if cached_response is not None:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            response_payload = deepcopy(cached_response)
            response_payload["status"] = "success"
            response_payload["cache_hit"] = True
            response_payload["elapsed_ms"] = elapsed_ms
            if request_id:
                response_payload["request_id"] = request_id
            self._emit_json(response_payload)
            return

        self.options.trim = payload.get("trim", "off")
        self.options.text_align = payload.get("text_align", "left")
        self.options.color_sort = payload.get("color_sort", "off")
        self.options.scale = payload.get("scale", 100)
        self.options.command_symbols = payload.get("command_symbols", False)
        self.options.letter_spacing = float(payload.get("letter_spacing", 0))
        self.options.word_spacing = float(payload.get("word_spacing", 0))
        self.options.line_height = float(payload.get("line_height", 0))
        self.options.text_position = payload.get("text_position", "left")
        self.draft_mode = bool(payload.get("draft_mode", False))
        include_preview = bool(payload.get("include_preview", False))
        self.include_preview = include_preview
        
        separator = payload.get("separator", "\n")
        texts = text_input.replace('\\n', '\n').split(separator)

        self.setup_trim()
        self.setup_text_align()
        self.setup_color_sort()
        self.setup_scale()

        self.metadata = self.get_inkstitch_metadata()
        self.collapse_len = self.metadata['collapse_len_mm']
        self.min_stitch_len = self.metadata['min_stitch_len_mm']

        # Draft mode trades some stitch fidelity for responsiveness during editing.
        if self.draft_mode:
            self.collapse_len *= 1.8
            self.min_stitch_len *= 1.6

        text_positioning_path = self.svg.findone(".//*[@inkscape:label='batch lettering']")

        try:
            self._emit_progress(request_id, "Gerando pontos no Ink/Stitch...", progress_pct=1)
            skip_stitch_computation = self._should_skip_stitch_plan_computation(file_formats)
            non_empty_count = len([text for text in texts if text])
            direct_svg_mode = include_preview and self.draft_mode and file_formats == ['svg'] and non_empty_count == 1
            direct_svg_content = None
            preview_payload = None
            zip_buffer = None if direct_svg_mode else io.BytesIO()
            zip_file = None if direct_svg_mode else ZipFile(zip_buffer, "w")
            processed_count = 0
            progress_state = {"pct": 1}
            progress_state_lock = threading.Lock()

            if not skip_stitch_computation and non_empty_count > 0:
                heartbeat_stop = threading.Event()
                heartbeat_started_at = time.perf_counter()

                def _heartbeat_loop():
                    while not heartbeat_stop.wait(3.0):
                        with progress_state_lock:
                            current_pct = int(max(1, min(94, progress_state["pct"])))
                        elapsed_seconds = int(time.perf_counter() - heartbeat_started_at)
                        self._emit_progress(
                            request_id,
                            f"Gerando pontos no Ink/Stitch... {elapsed_seconds}s",
                            progress_pct=current_pct,
                        )

                heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
                heartbeat_thread.start()

            for i, text in enumerate(texts):
                if not text:
                    continue

                if skip_stitch_computation:
                    stitch_plan = None
                    lettering_group = self._render_text_svg_only(text, text_positioning_path)
                else:
                    stitch_started_at = time.perf_counter()
                    stitch_plan, lettering_group = self.generate_stitch_plan(text, text_positioning_path)
                    stitch_time_total += (time.perf_counter() - stitch_started_at)

                if include_preview and stitch_plan is not None and preview_payload is None:
                    preview_payload = self._serialize_preview_payload(stitch_plan)

                if direct_svg_mode and direct_svg_content is None:
                    direct_svg_content = etree.tostring(self.document.getroot()).decode('utf-8')

                for file_format in file_formats:
                    if zip_file is not None:
                        file_name = self.build_output_file_name(text, i, file_format)
                        self.write_output_to_zip(zip_file, file_name, file_format, stitch_plan)

                self.reset_document(lettering_group, text_positioning_path)
                processed_count += 1
                if non_empty_count > 0:
                    progress_pct = int(2 + ((processed_count / non_empty_count) * 95))
                    with progress_state_lock:
                        progress_state["pct"] = progress_pct
                    self._emit_progress(request_id, "Gerando pontos no Ink/Stitch...", progress_pct=progress_pct)

            if heartbeat_stop is not None:
                heartbeat_stop.set()
                if heartbeat_thread is not None:
                    heartbeat_thread.join(timeout=0.2)

            response_payload = {
                "status": "success",
                "cache_hit": False,
                "draft_mode": self.draft_mode,
            }

            if direct_svg_mode:
                response_payload["svg_content"] = direct_svg_content or ""
            else:
                package_started_at = time.perf_counter()
                zip_file.close()
                zip_data = zip_buffer.getvalue()
                response_payload["zip_base64"] = base64.b64encode(zip_data).decode('utf-8')

            if preview_payload is not None:
                response_payload["preview_payload"] = preview_payload

            cache_payload = deepcopy(response_payload)
            cache_payload.pop("status", None)
            cache_payload.pop("cache_hit", None)
            cache_payload.pop("elapsed_ms", None)
            cache_payload.pop("stitch_ms", None)
            cache_payload.pop("package_ms", None)
            cache_payload.pop("request_id", None)
            self._set_cached_response(cache_key, cache_payload)

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            stitch_ms = int(stitch_time_total * 1000)
            package_ms = int(((time.perf_counter() - package_started_at) if package_started_at else 0.0) * 1000)
            response_payload["elapsed_ms"] = elapsed_ms
            response_payload["stitch_ms"] = stitch_ms
            response_payload["package_ms"] = package_ms
            if request_id:
                response_payload["request_id"] = request_id
            self._emit_json(response_payload)
        except Exception as e:
            self.send_error(str(e), request_id=request_id)
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()

    def setup_trim(self):
        self.trim = 0
        if self.options.trim == "line":
            self.trim = 1
        elif self.options.trim == "word":
            self.trim = 2
        elif self.options.trim == "glyph":
            self.trim = 3

    def setup_text_align(self):
        self.text_align = 0
        if self.options.text_align == "center":
            self.text_align = 1
        elif self.options.text_align == "right":
            self.text_align = 2
        elif self.options.text_align == "block":
            self.text_align = 3
        elif self.options.text_align == "letterspacing":
            self.text_align = 4

    def setup_color_sort(self):
        self.color_sort = 0
        if self.options.color_sort == "all":
            self.color_sort = 1
        elif self.options.color_sort == "line":
            self.color_sort = 2
        elif self.options.color_sort == "word":
            self.color_sort = 3

    def setup_scale(self):
        self.scale = self.options.scale / 100
        if self.scale < self.font.min_scale:
            self.scale = self.font.min_scale
        elif self.scale > self.font.max_scale:
            self.scale = self.font.max_scale

    def reset_document(self, lettering_group, text_positioning_path):
        parent = lettering_group.getparent()
        index = parent.index(lettering_group)
        if text_positioning_path is not None:
            parent.insert(index, text_positioning_path)
        lettering_group.delete()

    def build_output_file_name(self, text, iteration, file_format):
        allowed_characters = string.ascii_letters + string.digits
        filtered_text = ''.join(x for x in text if x in allowed_characters)
        if filtered_text:
            filtered_text = f'-{filtered_text}'
        file_name = f'{iteration:03d}{filtered_text:.8}'
        return f"{file_name}.{file_format}"

    def write_output_to_zip(self, zip_file, file_name, file_format, stitch_plan):
        if file_format == 'svg':
            document = deepcopy(self.document.getroot())
            zip_file.writestr(file_name, etree.tostring(document).decode('utf-8'))
            return

        if stitch_plan is None:
            raise ValueError(f"Cannot generate format '{file_format}' without stitch plan computation")

        temp_file = tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False)
        try:
            temp_file.close()
            write_embroidery_file(temp_file.name, stitch_plan, self.document.getroot())
            with open(temp_file.name, 'rb') as handle:
                zip_file.writestr(file_name, handle.read())
        finally:
            if os.path.exists(temp_file.name):
                os.remove(temp_file.name)

    def generate_stitch_plan(self, text, text_positioning_path):
        self.settings = DotDict({
            "text": text,
            "text_align": self.text_align,
            "back_and_forth": not self.draft_mode,
            "font": self.font.marked_custom_font_id,
            "scale": int(self.scale * 100),
            "trim_option": self.trim,
            "use_trim_symbols": self.options.command_symbols,
            "color_sort": self.color_sort,
            "letter_spacing": self.options.letter_spacing,
            "word_spacing": self.options.word_spacing,
            "line_height": self.options.line_height
        })

        lettering_group = Group()
        lettering_group.label = "Ink/Stitch Lettering"
        lettering_group.set('inkstitch:lettering', json.dumps(self.settings))
        self.svg.append(lettering_group)
        lettering_group.set("transform", get_correction_transform(lettering_group, child=True))

        destination_group = Group()
        destination_group.label = f"{self.font.name} scale {self.scale * 100}%"
        lettering_group.append(destination_group)

        text = self.font.render_text(
            text,
            destination_group,
            trim_option=self.trim,
            use_trim_symbols=self.options.command_symbols,
            color_sort=self.color_sort,
            text_align=self.text_align,
            letter_spacing=self.options.letter_spacing,
            word_spacing=self.options.word_spacing,
            line_height=self.options.line_height
        )

        destination_group.attrib['transform'] = f'scale({self.scale})'

        if text_positioning_path is not None:
            parent = text_positioning_path.getparent()
            index = parent.index(text_positioning_path)
            parent.insert(index, lettering_group)
            TextAlongPath(self.svg, lettering_group, text_positioning_path, self.options.text_position)
            text_positioning_path.delete()

        self.get_elements()
        stitch_groups = self.elements_to_stitch_groups(self.elements)
        stitch_plan = stitch_groups_to_stitch_plan(stitch_groups, collapse_len=self.collapse_len, min_stitch_len=self.min_stitch_len)
        ThreadCatalog().match_and_apply_palette(stitch_plan, self.get_inkstitch_metadata()['thread-palette'])

        return stitch_plan, lettering_group

if __name__ == '__main__':
    DaemonLettering().run()
