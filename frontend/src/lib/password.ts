/**
 * The password rules the API enforces (see `password_problem` on the server):
 * a minimum length, and at least three of lower-case, upper-case, digit, symbol.
 * Checked here only to guide the person typing; the API has the final word.
 *
 * The API states the minimum length on every screen that asks for a new password. The reset
 * screen is also given the other two numbers (GET /api/auth/password-policy); a screen that is
 * not given them uses the values below, which are the ones the API applies.
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

export interface PasswordLimits {
  maxLength?: number;
  /** How many of the four kinds of character a password needs. */
  requiredClasses?: number;
}

const COUNT_WORDS = ["none", "one", "two", "three", "four"];

export function checkPassword(password: string, minLength: number, limits: PasswordLimits = {}): PasswordCheck {
  const { maxLength = PASSWORD_MAX_LENGTH, requiredClasses = REQUIRED_CLASSES } = limits;
  const classes = {
    lower: /\p{Ll}/u.test(password),
    upper: /\p{Lu}/u.test(password),
    digit: /\p{Nd}/u.test(password),
    symbol: /[^\p{L}\p{N}]/u.test(password),
  };
  const classCount = Object.values(classes).filter(Boolean).length;
  const lengthOk = password.length >= minLength && password.length <= maxLength;
  const classesOk = classCount >= requiredClasses;
  return { lengthOk, classes, classCount, classesOk, valid: lengthOk && classesOk };
}

export function passwordProblem(password: string, minLength: number, limits: PasswordLimits = {}): string | null {
  const { maxLength = PASSWORD_MAX_LENGTH, requiredClasses = REQUIRED_CLASSES } = limits;
  if (password.length < minLength) return `Use at least ${minLength} characters.`;
  if (password.length > maxLength) return `Use at most ${maxLength} characters.`;
  if (!checkPassword(password, minLength, limits).classesOk) {
    const count = COUNT_WORDS[requiredClasses] ?? String(requiredClasses);
    return `Use at least ${count} of: lower-case letters, upper-case letters, digits, symbols.`;
  }
  return null;
}
