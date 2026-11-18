"""Deterministic Playwright adapters for the employer-hosted forms of Greenhouse, Lever, Ashby and Workable.

Each adapter: `inspect(page, url)` reads the form (labels, types, options, required; read-only: it types nothing and
clicks only dropdowns open and closed again to read their options), `fill(page, schema, answers, files)`,
`detect_blockers(page)`, `submit(page, guard)` (clicks the submit button exactly once) and `confirm(page)`.

The DOM reader is shared: the four systems render ordinary labelled inputs, native or React selects (role=combobox),
radio and checkbox groups and, on Ashby, Yes/No button pairs. Inputs that are not visible (honeypots) are never
read or filled, except file inputs, which every system hides behind an "Attach" button."""
import asyncio
import hashlib
import logging
import re

from ..browser_pool import _CHALLENGE
from .answers import match_option
from .schema import Blocker, FormField, FormSchema

log = logging.getLogger("jobfinder.autoapply")

EXTRACT_JS = r"""
(rootSelector) => {
  const doc = document;
  // The candidate container holding the most form controls (some boards have no <form>, or several small ones).
  const count = el => el.querySelectorAll('input:not([type=hidden]), select, textarea, [role="combobox"]').length;
  const candidates = Array.from(doc.querySelectorAll(rootSelector || 'form'));
  let root = candidates.sort((x, y) => count(y) - count(x))[0];
  if (!root || count(root) < 2) root = doc.body;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const text = el => {
    if (!el) return '';
    if (!el.querySelector || !el.querySelector('svg, script, style')) return clean(el.innerText || el.textContent || '');
    const c = el.cloneNode(true); c.querySelectorAll('svg, script, style').forEach(x => x.remove());
    return clean(c.textContent || '');
  };
  const controls = c => c.querySelectorAll('input:not([type=hidden]), select, textarea, [role="combobox"]').length;
  const visible = el => {
    if (!el) return false;
    const st = getComputedStyle(el); const r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' && parseFloat(st.opacity || '1') > 0.05 && r.width > 1 && r.height > 1;
  };
  const ownText = l => { const c = l.cloneNode(true); c.querySelectorAll('select, textarea, input, option, [role="listbox"]').forEach(x => x.remove()); return text(c); };
  const containerLabel = el => {
    let c = el.parentElement;
    for (let i = 0; i < 5 && c && c !== root.parentElement; i++, c = c.parentElement) {
      if (controls(c) > 1 && !(el.type === 'radio' || el.type === 'checkbox')) break;   // a label up there belongs to another field
      const l = c.querySelector(':scope > label, :scope > legend, :scope > .application-label, :scope > [class*="label" i], :scope > div > label, :scope > [class*="question" i] > label, :scope > [class*="title" i]');
      if (l && !l.contains(el) && text(l)) return text(l);
    }
    return '';
  };
  const labelOf = el => {
    let t = '';
    if (el.id) { const l = doc.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = text(l); }
    if (!t && el.getAttribute('aria-labelledby')) t = clean(el.getAttribute('aria-labelledby').split(/\s+/).map(i => text(doc.getElementById(i))).join(' '));
    if (!t && el.getAttribute('aria-label')) t = clean(el.getAttribute('aria-label'));
    if (!t) { const l = el.closest('label'); if (l) t = ownText(l); }
    if (!t) t = containerLabel(el);
    if (!t && el.placeholder) t = clean(el.placeholder);
    return t.slice(0, 600);
  };
  const groupLabel = (els) => {
    const first = els[0];
    const fs = first.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && text(lg)) return text(lg).slice(0, 600); }
    const grp = first.closest('[role="radiogroup"], [role="group"]');
    if (grp) {
      if (grp.getAttribute('aria-labelledby')) { const t = clean(grp.getAttribute('aria-labelledby').split(/\s+/).map(i => text(doc.getElementById(i))).join(' ')); if (t) return t.slice(0, 600); }
      if (grp.getAttribute('aria-label')) return clean(grp.getAttribute('aria-label')).slice(0, 600);
    }
    let c = first.parentElement;
    for (let i = 0; i < 6 && c; i++, c = c.parentElement) {
      if (els.every(e => c.contains(e))) {
        const l = c.querySelector(':scope > label, :scope > legend, :scope > div > label:not(:has(input)), :scope > [class*="label" i]:not(:has(input)), :scope > [class*="question" i], :scope > [class*="title" i], :scope > p, :scope > h3, :scope > h4');
        if (l && !els.some(e => l.contains(e)) && text(l)) return text(l).slice(0, 600);
      }
    }
    return '';
  };
  const optionLabel = el => {
    let t = '';
    if (el.id) { const l = doc.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = text(l); }
    if (!t) { const l = el.closest('label'); if (l) t = text(l); }
    if (!t && el.nextElementSibling) t = text(el.nextElementSibling);
    if (!t && el.parentElement) t = text(el.parentElement);
    return (t || el.value || '').slice(0, 300);
  };
  const isRequired = (el, label, holder) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    if (/[*\u2731\u2217]\s*$/.test(label) || /^\s*\*/.test(label)) return true;
    const h = holder || el.parentElement;
    const cls = ((h && h.className && h.className.toString()) || '') + ' ' + ((h && h.parentElement && h.parentElement.className && h.parentElement.className.toString()) || '');
    if (/\brequired\b/i.test(cls)) return true;
    const near = h && h.parentElement ? h.parentElement.querySelector('.required, [class*="required" i], abbr[title="required"]') : null;
    return !!near && !near.contains(el);
  };
  const sectionOf = el => {
    const c = el.closest('[id*="eeo" i], [class*="eeo" i], [class*="demographic" i], [id*="demographic" i], [class*="voluntary" i]');
    if (c) return 'eeo';
    const q = el.closest('[id*="question" i], [class*="custom" i], [class*="question" i], [class*="card" i]');
    return q ? 'custom' : 'standard';
  };
  const out = []; const used = new Set(); let n = 0;
  const keyFor = (base) => { let k = (base || ('aa_' + (n++))).slice(0, 120); let i = 2; while (used.has(k)) k = base + '__' + (i++); used.add(k); return k; };
  const tidy = l => clean(l.replace(/[\u2731\u2217*]+/g, ' ').replace(/\(required\)\s*$/i, ''));
  const handled = new Set();
  const all = Array.from(root.querySelectorAll('input, select, textarea, [role="combobox"]'));
  for (const el of all) {
    if (handled.has(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (el.disabled) continue;
    if (type === 'file') {
      // Upload buttons are labelled "Attach" / "Upload" / "Choose file": the question is the group's label.
      const generic = /^(attach|upload( file)?|choose( a)? file|browse|select file|drop|or drag)/i;
      let label = labelOf(el);
      if (!label || generic.test(label)) {
        let c = el.parentElement; let found = '';
        for (let i = 0; i < 6 && c && !found; i++, c = c.parentElement) {
          const l = c.querySelector('label, [class*="label" i], legend, [id$="-label"], h3, h4');
          if (l && !l.contains(el) && text(l) && !generic.test(text(l))) found = text(l);
          if (controls(c) > 3) break;
        }
        label = found || label || el.name || 'File';
      }
      const key = keyFor(el.id || el.name);
      el.setAttribute('data-aa-key', key);
      out.push({key, label: tidy(label), type: 'file', required: isRequired(el, label), options: [], section: sectionOf(el), multiple: false});
      continue;
    }
    if (!visible(el) && !(el.getAttribute('role') === 'combobox')) {
      if (!(type === 'radio' || type === 'checkbox')) continue;
      // custom-styled radios / checkboxes hide the native input; accept them when their label is visible
      const l = el.closest('label') || (el.id && doc.querySelector('label[for="' + CSS.escape(el.id) + '"]'));
      if (!l || !visible(l)) continue;
    }
    if (type === 'radio' || type === 'checkbox') {
      const name = el.name;
      let group = name ? all.filter(e => e.name === name && (e.getAttribute('type') || '').toLowerCase() === type) : [el];
      group.forEach(e => handled.add(e));
      const options = group.map(optionLabel);
      if (type === 'checkbox' && group.length === 1) {
        const label = optionLabel(el) || labelOf(el);
        const key = keyFor(el.id || name);
        el.setAttribute('data-aa-key', key);
        out.push({key, label: tidy(label), type: 'checkbox', required: isRequired(el, label), options: [], section: sectionOf(el), multiple: false});
        continue;
      }
      const label = groupLabel(group) || labelOf(el);
      const key = keyFor(name || el.id);
      group.forEach(e => e.setAttribute('data-aa-key', key));
      out.push({key, label: tidy(label), type: type === 'radio' ? 'radio' : 'checkbox', required: group.some(e => isRequired(e, label, e.closest('fieldset') || e.parentElement)),
                options, section: sectionOf(el), multiple: type === 'checkbox'});
      continue;
    }
    if (el.getAttribute('role') === 'combobox' && tag !== 'select') {
      const label = labelOf(el);
      const key = keyFor(el.id || el.name);
      el.setAttribute('data-aa-key', key);
      out.push({key, label: tidy(label), type: 'select', required: isRequired(el, label), options: [], section: sectionOf(el), multiple: false, combobox: true});
      continue;
    }
    const label = labelOf(el);
    const key = keyFor(el.id || el.name);
    el.setAttribute('data-aa-key', key);
    if (tag === 'select') {
      const options = Array.from(el.options).filter(o => o.value !== '' && !/^\s*(select|choose|please select|--)/i.test(o.text)).map(o => clean(o.text));
      out.push({key, label: tidy(label), type: 'select', required: isRequired(el, label), options, section: sectionOf(el), multiple: !!el.multiple});
      continue;
    }
    const kind = tag === 'textarea' ? 'textarea' : (['email', 'tel', 'url', 'date', 'number'].includes(type) ? type : 'text');
    out.push({key, label: tidy(label), type: kind, required: isRequired(el, label), options: [], section: sectionOf(el), multiple: false});
  }
  // Ashby-style Yes/No button pairs
  const buttons = Array.from(root.querySelectorAll('button[type="button"]')).filter(b => /^(yes|no)$/i.test(text(b)) && visible(b));
  const parents = new Set(buttons.map(b => b.parentElement));
  for (const p of parents) {
    const pair = Array.from(p.querySelectorAll(':scope > button[type="button"]')).filter(b => /^(yes|no)$/i.test(text(b)));
    if (pair.length !== 2) continue;
    const label = groupLabel(pair) || containerLabel(p);
    const key = keyFor(p.id || ('yesno_' + hashish(label)));
    p.setAttribute('data-aa-key', key);
    out.push({key, label: tidy(label), type: 'radio', required: isRequired(pair[0], label, p), options: pair.map(text), section: sectionOf(p), multiple: false, buttons: true});
  }
  function hashish(s) { let h = 0; for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) | 0; return (h >>> 0).toString(36); }
  return out;
}
"""

BLOCKER_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const st = getComputedStyle(el); const r = el.getBoundingClientRect(); return st.display !== 'none' && st.visibility !== 'hidden' && parseFloat(st.opacity || '1') > 0.05 && r.width > 1 && r.height > 1 ? [r.width, r.height] : null; };
  const frames = Array.from(document.querySelectorAll('iframe')).map(f => ({src: f.src || '', title: f.title || '', size: vis(f), id: f.id || ''}));
  const password = Array.from(document.querySelectorAll('input[type="password"]')).some(el => !!vis(el));
  const codeInputs = Array.from(document.querySelectorAll('input[autocomplete="one-time-code"], input[name*="code" i], input[id*="code" i]')).some(el => !!vis(el));
  return {frames, password, codeInputs, title: document.title || '', text: clean(document.body ? document.body.innerText : '').slice(0, 30000), url: location.href};
}
"""

CAPTCHA_SRC = re.compile(r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha|geetest|captcha", re.I)
SIGN_IN = re.compile(r"\b(sign|log) ?in to (apply|continue)|please (sign|log) in|you must (sign|log) in|login required|already have an account\? ?(sign|log) in", re.I)
ACCOUNT = re.compile(r"create (an|your) account (to|before) (apply|continu)|register to apply|sign up to apply", re.I)
EMAIL_CODE = re.compile(r"(enter|type) the (\d+.digit )?(verification |security |one.time )?code|we('ve| have)? sent (you )?(a|an) (\d+.digit )?(verification |security )?code|verify your e-?mail", re.I)
PUZZLE = re.compile(r"prove (that )?you('re| are) (not a (bot|robot)|human)|not a bot auto.?apply|are you (a )?human|solve (the|this) (puzzle|challenge)|anti.?bot", re.I)
CLOSED = re.compile(r"no longer (accepting applications|available|open|active)|position has been filled|job (is )?(not found|closed|expired)|"
                    r"posting (has been )?(closed|removed)|page (you('re| are) looking for )?(could not be|was not|wasn't) found|couldn.?t find (that|this) job", re.I)
CONFIRM = re.compile(r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?(successfully )?(submitted|received|sent)|"
                     r"we('ve| have) received your application|your application (is|has been) (in|complete)|successfully applied", re.I)
REFERENCE = re.compile(r"(?:reference|confirmation|application)\s*(?:number|no\.?|id|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,40})", re.I)
FIELD_ERROR = re.compile(r"(this field|field) is required|can.?t be blank|is required|please (fill|complete|enter)|invalid", re.I)


class Adapter:
    name = "generic"
    root_selector = "form"
    submit_text = re.compile(r"^\s*(submit( (your )?application)?|apply( now)?|send application|submit & apply)\s*$", re.I)
    hosts: tuple[str, ...] = ()

    # ---------------------------------------------------------------- inspect
    async def open(self, page, url: str, timeout: float = 30.0) -> int:
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except PlaywrightTimeout:
            return 0
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except PlaywrightTimeout:
            pass
        await self.reveal_form(page)
        return response.status if response else 0

    async def reveal_form(self, page) -> None:
        """Some boards show the posting first and the form after an "Apply" click on the same page (no request is sent)."""
        return None

    async def inspect(self, page, url: str) -> FormSchema:
        schema = FormSchema(ats=self.name, url=url)
        schema.title = (await page.title())[:200]
        schema.blockers = await self.detect_blockers(page, before_form=True)
        if any(b.code in ("challenge_page", "posting_closed", "sign_in_required", "account_creation_required", "employer_iframe",
                          "custom_career_site") for b in schema.blockers):
            return schema
        raw = await page.evaluate(EXTRACT_JS, self.root_selector)
        fields: list[FormField] = []
        for item in raw:
            f = FormField(key=item["key"], label=item["label"] or item["key"], type=item["type"], required=bool(item["required"]),
                          options=[o for o in item.get("options") or [] if o][:200], section=item.get("section") or "",
                          selector=f'[data-aa-key="{item["key"]}"]', multiple=bool(item.get("multiple")))
            if item.get("combobox"):
                f.selector += "|combobox"
                f.options = await self._combobox_options(page, f)
            if item.get("buttons"):
                f.selector += "|buttons"
            fields.append(f)
        if not any(f.type == "email" or re.search(r"e-?mail", f.label, re.I) for f in fields):
            schema.blockers.append(Blocker("form_not_found", "No application form was found on the page"))
        schema.fields = fields
        return schema

    @staticmethod
    def _css(f: FormField) -> str:
        return f.selector.split("|")[0]

    async def _combobox_options(self, page, f: FormField) -> list[str]:
        """Open a React select, read its options, close it. Sends nothing (remote autocompletes answer only to typing)."""
        try:
            box = page.locator(self._css(f)).first
            await box.click(timeout=3000)
            await asyncio.sleep(0.4)
            options = await page.evaluate("""(css) => {
                const box = document.querySelector(css);
                const ids = ((box && (box.getAttribute('aria-controls') || box.getAttribute('aria-owns'))) || '').split(/\\s+/).filter(Boolean);
                let opts = [];
                for (const id of ids) { const l = document.getElementById(id); if (l) opts = opts.concat(Array.from(l.querySelectorAll('[role="option"]'))); }
                if (!opts.length) opts = Array.from(document.querySelectorAll('[role="option"]')).filter(o => {
                    const r = o.getBoundingClientRect(); const st = getComputedStyle(o); return r.width > 0 && r.height > 0 && st.visibility !== 'hidden'; });
                return opts.map(o => (o.innerText || o.textContent || '').replace(/\\s+/g, ' ').trim()).filter(Boolean).slice(0, 200);
            }""", self._css(f))
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.1)
            return [o for o in options if not re.match(r"^(no options|loading)", o, re.I)]
        except Exception:
            return []

    # ---------------------------------------------------------------- blockers
    async def detect_blockers(self, page, before_form: bool = False) -> list[Blocker]:
        info = await page.evaluate(BLOCKER_JS)
        out: list[Blocker] = []
        text, title = info["text"], info["title"]
        if _CHALLENGE.search(title or "") and not re.search(r"apply|application|job", title or "", re.I):
            out.append(Blocker("challenge_page", title[:120]))
        for frame in info["frames"]:
            src = frame["src"]
            if not CAPTCHA_SRC.search(src + " " + frame["title"]):
                continue
            size = frame["size"]
            if not size:
                continue
            invisible = "size=invisible" in src
            challenge = re.search(r"bframe|challenge|hcaptcha\.com/.*#frame=challenge", src, re.I)
            if (challenge and size[0] > 100 and size[1] > 100) or (not invisible and not challenge and size[0] > 60 and size[1] > 40):
                out.append(Blocker("captcha_visible", re.sub(r"\?.*$", "", src)[:150]))
                break
        if info["password"]:
            out.append(Blocker("sign_in_required", "A password field is shown"))
        elif SIGN_IN.search(text):
            out.append(Blocker("sign_in_required", SIGN_IN.search(text).group(0)))
        if ACCOUNT.search(text):
            out.append(Blocker("account_creation_required", ACCOUNT.search(text).group(0)))
        if EMAIL_CODE.search(text) and (info["codeInputs"] or not before_form):
            out.append(Blocker("email_code_required", EMAIL_CODE.search(text).group(0)))
        if PUZZLE.search(text):
            out.append(Blocker("anti_bot_puzzle", PUZZLE.search(text).group(0)))
        if before_form and CLOSED.search(text[:4000]):
            out.append(Blocker("posting_closed", CLOSED.search(text[:4000]).group(0)))
        return out

    # ---------------------------------------------------------------- fill
    async def fill(self, page, schema: FormSchema, answers: list[dict], files: dict[str, tuple[str, str, bytes]]) -> list[dict]:
        """Type the answers. `files`: field_key -> (name, mime type, bytes). Returns the fill report."""
        by_key = {f.key: f for f in schema.fields}
        report = []
        for a in answers:
            f = by_key.get(a["field_key"])
            value = a.get("value") or ""
            if f is None or a.get("status") == "blocked" or (value == "" and f.type != "checkbox") or value == "unchecked":
                continue
            entry = {"field_key": f.key, "label": f.label[:200], "value": value if f.type != "file" else files.get(f.key, ("",))[0],
                     "filled": False, "error": ""}
            try:
                await self._fill_one(page, f, value, files.get(f.key))
                entry["filled"] = True
            except Exception as exc:
                entry["error"] = (str(exc).splitlines() or [type(exc).__name__])[0][:200]
            report.append(entry)
        return report

    async def _fill_one(self, page, f: FormField, value: str, file: tuple | None) -> None:
        css = self._css(f)
        loc = page.locator(css)
        if f.type == "file":
            if not file:
                raise ValueError("no file")
            name, mime, data = file
            await loc.first.set_input_files(files=[{"name": name, "mimeType": mime, "buffer": data}])
            return
        if "|combobox" in f.selector:
            box = loc.first
            await box.click()
            await box.fill("")
            await page.keyboard.type(value[:80], delay=15)
            await asyncio.sleep(0.6)
            options = page.locator('[role="option"]:visible')
            count = await options.count()
            for i in range(count):
                text = " ".join((await options.nth(i).inner_text()).split())
                if text.lower() == value.lower():
                    await options.nth(i).click()
                    return
            await page.keyboard.press("Escape")
            raise ValueError(f"option {value!r} not offered")
        if "|buttons" in f.selector:
            await loc.first.locator("button", has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)).first.click()
            return
        if f.type == "select":
            await loc.first.select_option(label=value)
            return
        if f.type == "radio":
            await self._click_option(page, css, value)
            return
        if f.type == "checkbox":
            if f.multiple:
                for part in [p.strip() for p in value.split(";") if p.strip()]:
                    await self._click_option(page, css, part, check=True)
                return
            if value == "checked":
                await loc.first.check(force=True)
            return
        await loc.first.fill(value)

    async def _click_option(self, page, css: str, value: str, check: bool = False) -> None:
        handles = await page.query_selector_all(css)
        labels = []
        for h in handles:
            label = await h.evaluate("""el => { let t=''; if (el.id) { const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if (l) t=l.innerText; }
                if (!t) { const l=el.closest('label'); if (l) t=l.innerText; } if (!t && el.nextElementSibling) t=el.nextElementSibling.innerText;
                if (!t && el.parentElement) t=el.parentElement.innerText; return (t||el.value||'').replace(/\\s+/g,' ').trim(); }""")
            labels.append(label)
        chosen = match_option(value, labels)
        if chosen is None:
            raise ValueError(f"option {value!r} not offered")
        handle = handles[labels.index(chosen)]
        try:
            await handle.check(force=True, timeout=4000)
        except Exception:
            # Custom-styled controls: the native input is hidden and only its label (or wrapper) takes the click.
            clicked = await handle.evaluate("""el => { const l = (el.id && document.querySelector('label[for="'+CSS.escape(el.id)+'"]'))
                || el.closest('label') || el.parentElement; if (!l) return false; l.click(); return true; }""")
            await asyncio.sleep(0.2)
            if not clicked or not await handle.evaluate("el => el.checked"):
                raise ValueError(f"option {value!r} could not be selected") from None

    # ---------------------------------------------------------------- submit and confirm
    async def submit_button(self, page):
        root = page.locator(self.root_selector).first if await page.locator(self.root_selector).count() else page.locator("body")
        for sel in ('button[type="submit"]', 'input[type="submit"]', "button"):
            buttons = root.locator(sel)
            for i in range(await buttons.count()):
                b = buttons.nth(i)
                try:
                    if not await b.is_visible():
                        continue
                    label = (await b.inner_text()) if sel != 'input[type="submit"]' else (await b.get_attribute("value") or "")
                except Exception:
                    continue
                if self.submit_text.match(" ".join(label.split())):
                    return b
        return None

    async def submit(self, page, guard) -> dict:
        """Click the submit button exactly once. Never repeated, whatever happens next."""
        button = await self.submit_button(page)
        if button is None:
            return {"clicked": False, "error": "submit button not found"}
        before = guard.allowed_submissions
        await button.click(timeout=10000, no_wait_after=True)
        for _ in range(30):                                    # up to ~15 s for the answer page
            await asyncio.sleep(0.5)
            if CONFIRM.search(await self._text(page)):
                break
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        return {"clicked": True, "requests": guard.allowed_submissions - before}

    @staticmethod
    async def _text(page) -> str:
        try:
            return " ".join((await page.evaluate("() => document.body ? document.body.innerText : ''") or "").split())[:30000]
        except Exception:
            return ""

    async def confirm(self, page) -> dict:
        text = await self._text(page)
        hit = CONFIRM.search(text)
        reference = REFERENCE.search(text) if hit else None
        errors = bool(FIELD_ERROR.search(text)) and not hit
        return {"detected": bool(hit), "text_sha256": hashlib.sha256(text.encode()).hexdigest() if hit else "",
                "phrase": hit.group(0)[:120] if hit else "", "reference": reference.group(1)[:100] if reference else "",
                "url": re.sub(r"\?.*$", "", page.url)[:300], "validation_errors": errors}


class GreenhouseAdapter(Adapter):
    name = "greenhouse"
    root_selector = "#application-form, form#application_form, #application_form, form[action*='applications'], form"

    async def detect_blockers(self, page, before_form: bool = False) -> list[Blocker]:
        out = await super().detect_blockers(page, before_form)
        host = re.sub(r"^https?://([^/]+).*$", r"\1", page.url).lower()
        if before_form and "greenhouse.io" not in host:
            out.append(Blocker("custom_career_site", f"The posting redirected to {host}"))
        if before_form and await page.locator("iframe#grnhse_iframe, iframe[src*='greenhouse.io/embed']").count():
            out.append(Blocker("employer_iframe", "The form is embedded in the employer's own page"))
        if before_form and re.search(r"[?&]error=true", page.url):
            out.append(Blocker("posting_closed", "Greenhouse redirected to the job board (posting closed)"))
        return out

    async def reveal_form(self, page) -> None:
        # New job boards show the form below the description; older ones behind an "Apply for this job" anchor (#app).
        if await page.locator("#application-form, #application_form, form#application_form").count():
            return
        link = page.locator("a[href='#app'], button:has-text('Apply for this job'), a:has-text('Apply for this job')")
        if await link.count():
            try:
                await link.first.click(timeout=3000, no_wait_after=True)
                await asyncio.sleep(0.8)
            except Exception:
                pass


class LeverAdapter(Adapter):
    name = "lever"
    root_selector = "form#application-form, form[action*='/apply'], .application-form form, form"
    submit_text = re.compile(r"^\s*(submit( your)? application|submit|apply)\s*$", re.I)


class AshbyAdapter(Adapter):
    name = "ashby"
    root_selector = "form, [class*='application-form' i], [class*='ApplicationForm' i], main"
    submit_text = re.compile(r"^\s*(submit( application)?|apply)\s*$", re.I)

    async def reveal_form(self, page) -> None:
        tab = page.locator("a[href$='/application'], button:has-text('Application'), [role='tab']:has-text('Application')")
        if not await page.locator("input[type='file'], input[type='email']").count() and await tab.count():
            try:
                await tab.first.click(timeout=3000, no_wait_after=True)
                await asyncio.sleep(1.0)
            except Exception:
                pass


class WorkableAdapter(Adapter):
    name = "workable"
    root_selector = "form[data-ui='application-form'], form"
    submit_text = re.compile(r"^\s*(submit( application)?|apply|send application)\s*$", re.I)


ADAPTERS = {a.name: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), WorkableAdapter())}


def adapter_for(ats: str) -> Adapter | None:
    return ADAPTERS.get(ats)
