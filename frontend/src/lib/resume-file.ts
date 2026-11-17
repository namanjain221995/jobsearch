import { formatBytes } from "./format";

/** What POST /api/candidates/{id}/resume accepts: PDF, DOCX or TXT, up to 10 MB. Checked here first, so a wrong file never travels. */
export const RESUME_MAX_BYTES = 10 * 1024 * 1024;
export const RESUME_EXTENSIONS = [".pdf", ".docx", ".txt"];
export const RESUME_ACCEPT =
  ".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain";
export const RESUME_FORMATS = "PDF, DOCX or TXT, up to 10 MB";

/** A sentence saying why this file cannot be a resume, or null when it can. */
export function resumeFileProblem(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!RESUME_EXTENSIONS.some((extension) => name.endsWith(extension))) return "Choose a PDF, DOCX or TXT file.";
  if (file.size > RESUME_MAX_BYTES) return `This file is ${formatBytes(file.size)}. The limit is 10 MB.`;
  if (file.size === 0) return "This file is empty.";
  return null;
}
