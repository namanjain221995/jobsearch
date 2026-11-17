/**
 * Job descriptions arrive as plain text: line breaks, and "• " at the start of a
 * bullet. This module turns that text into paragraphs, lists and sections so it
 * can be rendered as React text nodes. It never produces HTML.
 *
 * A line becomes a section heading only when the text itself offers one: a short
 * line, without sentence punctuation, that reads like a heading postings
 * actually use (About..., Responsibilities, Required Qualifications, Benefits,
 * How to Apply, ...). Everything else stays exactly as written. No section is
 * ever invented.
 */

export type DescriptionBlock = { type: "paragraph"; text: string } | { type: "list"; items: string[] };

export interface DescriptionSection {
  /** null for text that comes before the first heading (or when the posting has no headings at all). */
  heading: string | null;
  blocks: DescriptionBlock[];
}

const BULLET = /^\s*(?:[•◦▪‣·●○■]\s*|[-–—*]\s+)/u;

const HEADING_PATTERNS: RegExp[] = [
  /^about(?:$|\s+(?!\d))/,
  /^(?:company|role|job|position|team|program)\s+(?:overview|summary|description|details|purpose)$/,
  /^(?:overview|summary|description|introduction|the (?:role|opportunity|position|job|team|company)|our (?:team|company|mission|values|story|culture))$/,
  /^who (?:we are|you are|we're looking for|we are looking for)$/,
  /^what (?:you(?:'|’)?ll|you will|you|we(?:'|’)?re|we are|we) (?:do|be doing|bring|need|have|offer|provide|looking for|look for|get|work on)\b/,
  /^(?:your|the|key|main|core|primary|essential|job|day[- ]to[- ]day)?\s*(?:responsibilities|duties|functions|duties and responsibilities|responsibilities and duties|accountabilities)$/,
  /^(?:essential|key) (?:functions|duties|job functions)$/,
  /^(?:the |job |minimum |basic |required |preferred |desired |additional |other |key |technical |education(?:al)? |position )*(?:requirements|qualifications|skills|experience|education|competencies|knowledge|abilities|credentials|certifications)(?: (?:and|&) (?:requirements|qualifications|skills|experience|education|abilities))?$/,
  /^(?:must[- ]haves?|nice[- ]to[- ]haves?|bonus points|pluses|plus if you have|preferred|required|desirable)$/,
  /^(?:skills|required skills|preferred skills|technical skills|key skills|skills (?:and|&) (?:experience|qualifications|abilities))$/,
  /^(?:benefits|perks|perks (?:and|&) benefits|benefits (?:and|&) perks|what we offer|why join us|why (?:us|work (?:here|with us))|total rewards)$/,
  /^(?:compensation|salary|pay|pay range|salary range|base salary|compensation range|pay transparency|compensation (?:and|&) benefits|salary (?:and|&) benefits)$/,
  /^(?:work authorization|employment authorization|sponsorship|visa sponsorship|work eligibility|eligibility|security clearance|clearance requirements?)$/,
  /^(?:location|work location|work arrangement|work environment|working conditions|schedule|hours|travel|travel requirements?|physical requirements?|physical demands)$/,
  /^(?:how to apply|application process|interview process|hiring process|next steps|to apply)$/,
  /^(?:equal (?:employment )?opportunity(?: employer| statement)?|eeo(?: statement)?|diversity(?:,| and| &)? (?:equity(?:,| and| &)? )?(?:and )?inclusion|accommodations?|additional information|other information|notes?)$/,
];

function normalizeHeading(line: string): string {
  return line
    .trim()
    .replace(/[:：]\s*$/, "")
    .replace(/\s+/g, " ")
    .toLowerCase();
}

/** True when the line is short, unpunctuated and reads like a heading real postings use. */
export function isHeadingLine(line: string): boolean {
  const text = line.trim();
  if (!text || text.length > 70 || BULLET.test(text)) return false;
  if (/[.,;!?]$/.test(text)) return false;
  if (text.split(/\s+/).length > 8) return false;
  const normalized = normalizeHeading(text);
  return HEADING_PATTERNS.some((pattern) => pattern.test(normalized));
}

export function parseDescription(source: string | null | undefined): DescriptionSection[] {
  const lines = (source ?? "").replace(/\r\n?/g, "\n").replace(/\u0000/g, "").split("\n");
  const sections: DescriptionSection[] = [];
  let current: DescriptionSection = { heading: null, blocks: [] };
  let paragraph: string[] = [];
  let list: string[] | null = null;

  const flushParagraph = () => {
    if (paragraph.length) current.blocks.push({ type: "paragraph", text: paragraph.join("\n") });
    paragraph = [];
  };
  const flushList = () => {
    if (list && list.length) current.blocks.push({ type: "list", items: list });
    list = null;
  };
  const closeSection = () => {
    flushParagraph();
    flushList();
    if (current.heading !== null || current.blocks.length) sections.push(current);
  };
  const nextContentIndex = (from: number) => {
    for (let i = from; i < lines.length; i += 1) if (lines[i].trim()) return i;
    return -1;
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].replace(/\s+$/, "");
    const text = line.trim();

    if (!text) {
      flushParagraph();
      // A blank line between two bullets does not end the list.
      const next = nextContentIndex(index + 1);
      if (list && (next === -1 || !BULLET.test(lines[next]))) flushList();
      continue;
    }

    if (BULLET.test(line)) {
      flushParagraph();
      const item = line.replace(BULLET, "").trim();
      if (item) (list ??= []).push(item);
      continue;
    }

    const next = nextContentIndex(index + 1);
    if (isHeadingLine(text) && next !== -1 && !isHeadingLine(lines[next])) {
      closeSection();
      current = { heading: text.replace(/[:：]\s*$/, ""), blocks: [] };
      continue;
    }

    flushList();
    paragraph.push(text);
  }
  closeSection();
  return sections;
}
