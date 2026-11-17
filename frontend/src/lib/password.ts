/**
 * The password rules the API enforces (see `password_problem` on the server):
 * a minimum length, and at least three of lower-case, upper-case, digit, symbol.
 * Checked here only to guide the person typing; the API has the final word.
 */
export interface PasswordCheck {
  lengthOk: boolean;
  classes: { lower: boolean; upper: boolean; digit: boolean; symbol: boolean };
  classCount: number;
  classesOk: boolean;
  valid: boolean;
}

export const PASSWORD_MAX_LENGTH = 128;
export const REQUIRED_CLASSES = 3;

export function checkPassword(password: string, minLength: number): PasswordCheck {
  const classes = {
    lower: /\p{Ll}/u.test(password),
    upper: /\p{Lu}/u.test(password),
    digit: /\p{Nd}/u.test(password),
    symbol: /[^\p{L}\p{N}]/u.test(password),
  };
  const classCount = Object.values(classes).filter(Boolean).length;
  const lengthOk = password.length >= minLength && password.length <= PASSWORD_MAX_LENGTH;
  const classesOk = classCount >= REQUIRED_CLASSES;
  return { lengthOk, classes, classCount, classesOk, valid: lengthOk && classesOk };
}

export function passwordProblem(password: string, minLength: number): string | null {
  if (password.length < minLength) return `Use at least ${minLength} characters.`;
  if (password.length > PASSWORD_MAX_LENGTH) return `Use at most ${PASSWORD_MAX_LENGTH} characters.`;
  if (!checkPassword(password, minLength).classesOk) {
    return "Use at least three of: lower-case letters, upper-case letters, digits, symbols.";
  }
  return null;
}
