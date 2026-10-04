// Shared priority/issue_type/report_channel filter dropdowns, styled like the dimension
// picker in the "Dimension values" panel. Options come from the server's own
// list_dimension_values, so a selected value is always one the server
// recognizes — no free-text entry, no typos, no stray quote characters.
const FIELDS = [
  { key: 'priority', label: 'priority filter' },
  { key: 'issue_type', label: 'issue type filter' },
  { key: 'report_channel', label: 'report channel filter' },
]

export default function FilterFields({ options = {}, values, onChange }) {
  function selectField(key, label) {
    const choices = options[key] || []
    return (
      <div className="field" key={key}>
        <label>{label}</label>
        <select value={values[key]} onChange={(event) => onChange({ ...values, [key]: event.target.value })}>
          <option value="">(any)</option>
          {choices.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.value} ({choice.case_count})
            </option>
          ))}
        </select>
      </div>
    )
  }

  return <div className="row">{FIELDS.map(({ key, label }) => selectField(key, label))}</div>
}
