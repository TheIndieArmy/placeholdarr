/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      fontFamily: {
        headline: ['"Space Grotesk"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        body: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        label: ['"Space Grotesk"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"Fira Code"', 'ui-monospace', 'monospace'],
        'brand-head': ['var(--brand-font-headline)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        'brand-body': ['var(--brand-font-body)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        'brand-label': ['var(--brand-font-label)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        'brand-mono': ['var(--brand-font-mono)', 'ui-monospace', 'monospace'],
      },
      colors: {
        // Legacy slate hex aliases (Mirarr-era); prefer `var(--brand-*)` in new UI — see `brandSemanticTheme.ts`
        background: '#0f1419',
        'surface': '#171c22',
        'surface-container': '#1e2530',
        'surface-container-low': '#1a2030',
        'surface-container-high': '#252e3a',
        'surface-container-highest': '#2e3845',
        'on-surface': '#dee3eb',
        'on-background': '#dee3eb',
        'outline-variant': '#424753',
        'primary': '#4f7ef7',
        'primary-container': '#1d3461',
        'primary-fixed-dim': '#4f7ef7',
        'on-primary': '#ffffff',
        'on-primary-fixed': '#ffffff',
        'on-primary-container': '#adc6ff',
        'secondary': '#7dd3fc',
        'error': '#ffb4ab',
        'error-container': '#93000a',
      },
      animation: {
        'status-pulse': 'status-pulse 2s ease-in-out infinite',
      },
      keyframes: {
        'status-pulse': {
          '0%, 100%': { opacity: '1', boxShadow: '0 0 0 0 rgba(79,126,247,0.4)' },
          '50%': { opacity: '0.8', boxShadow: '0 0 0 6px rgba(79,126,247,0)' },
        },
      },
    },
  },
  plugins: [
    require('@tailwindcss/forms'),
  ],
}
