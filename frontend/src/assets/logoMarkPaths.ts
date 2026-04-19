/**
 * v1.1 mark geometry (viewBox 0 0 72 72): curly braces framing a centered rounded play.
 * Shared geometry for in-app `BrandLogo` marks.
 */

/** Left “{” stroke (open toward center). */
export const LOGO_CURLY_LEFT_D =
  "M 22 10 C 12 10 9 20 9 30 C 9 33 6 34.5 6 36 C 6 37.5 9 39 9 42 C 9 52 12 62 22 62";

/** Right “}” stroke (mirror of left). */
export const LOGO_CURLY_RIGHT_D =
  "M 50 10 C 60 10 63 20 63 30 C 63 33 66 34.5 66 36 C 66 37.5 63 39 63 42 C 63 52 60 62 50 62";

/** Rounded play triangle; centroid at (36, 36) (flat left edge, tip on the right). */
export const LOGO_PLAY_ROUNDED_D =
  "M 30 26.5 Q 29.25 26.5 29.25 27.25 L 29.25 44.75 Q 29.25 45.5 30 45.5 Q 30.55 45.5 31.15 45.15 L 47.05 36.35 Q 47.95 36 47.05 35.65 L 31.15 26.85 Q 30.55 26.5 30 26.5 Z";
