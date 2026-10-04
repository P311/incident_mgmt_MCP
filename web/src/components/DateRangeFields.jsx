export default function DateRangeFields({ from, to, onFromChange, onToChange }) {
  return (
    <>
      <div className="field">
        <label>from</label>
        <input type="date" value={from} onChange={(event) => onFromChange(event.target.value)} />
      </div>
      <div className="field">
        <label>to</label>
        <input type="date" value={to} onChange={(event) => onToChange(event.target.value)} />
      </div>
    </>
  )
}
