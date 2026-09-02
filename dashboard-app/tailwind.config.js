/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        accent: '#4C6FFF',
        canvas: '#F4F6FB',
        ink: '#1C2030',
        muted: '#8A8FA6',
      },
      boxShadow: {
        card: '0 2px 10px rgba(28, 32, 48, 0.05)',
        modal: '0 24px 70px rgba(20, 24, 38, 0.22)',
      },
      borderRadius: {
        xl2: '14px',
      },
    },
  },
  plugins: [],
};
