export default function ErrorBanner({ error }) {
  if (!error) return null
  const text = error.hint ? `${error.message}\n\n${error.hint}` : error.message
  return <div className="error">{text}</div>
}
