import React, {useId} from 'react';

type FieldLabel = {label: string; error?: string};

export function CheckboxGroup({label, value, options, onChange, error, ...props}:
  Omit<React.FieldsetHTMLAttributes<HTMLFieldSetElement>, 'onChange'> & FieldLabel & {
    value: string[]; onChange: (value: string[]) => void;
    options: {value: string; label: string; disabled?: boolean}[];
  }) {
  const id=useId(), errorId=`${id}-error`;
  return <fieldset {...props} aria-invalid={error ? true : props['aria-invalid']}
    aria-describedby={[props['aria-describedby'],error && errorId].filter(Boolean).join(' ') || undefined}>
    <legend>{label}</legend>
    {options.map((option,index)=><label key={option.value} htmlFor={`${id}-${index}`}>
      <input id={`${id}-${index}`} type="checkbox" value={option.value}
        disabled={option.disabled} checked={value.includes(option.value)} onChange={event=>onChange(
          event.target.checked ? [...new Set([...value,option.value])] : value.filter(item=>item!==option.value)
        )}/>{option.label}
    </label>)}
    {error && <span id={errorId} role="alert">{error}</span>}
  </fieldset>;
}

// Single-selection native control. Multi-select/listbox needs its own contract.
export function SelectField({label, error, id, ...props}:
  Omit<React.SelectHTMLAttributes<HTMLSelectElement>, 'multiple' | 'size'> & FieldLabel) {
  const generated = useId(), inputId = id ?? generated, errorId = `${inputId}-error`;
  return <div><label htmlFor={inputId}>{label}</label>
    <select {...props} multiple={false} size={undefined} id={inputId}
      aria-invalid={error ? true : props['aria-invalid']}
      aria-describedby={[props['aria-describedby'], error && errorId].filter(Boolean).join(' ') || undefined}/>
    {error && <span id={errorId} role="alert">{error}</span>}
  </div>;
}

export function TextAreaField({label, error, id, ...props}:
  React.TextareaHTMLAttributes<HTMLTextAreaElement> & FieldLabel) {
  const generated = useId(), inputId = id ?? generated, errorId = `${inputId}-error`;
  return <div><label htmlFor={inputId}>{label}</label>
    <textarea {...props} id={inputId} aria-invalid={error ? true : props['aria-invalid']}
      aria-describedby={[props['aria-describedby'], error && errorId].filter(Boolean).join(' ') || undefined}/>
    {error && <span id={errorId} role="alert">{error}</span>}
  </div>;
}

export function RadioGroup({label, value, options, onChange, name, error, ...props}:
  Omit<React.FieldsetHTMLAttributes<HTMLFieldSetElement>, 'onChange'> & FieldLabel & {
    name?: string; value: string; onChange: (value: string) => void;
    options: {value: string; label: string; disabled?: boolean}[];
  }) {
  const id = useId(), groupName = name ?? id, errorId = `${id}-error`;
  return <fieldset {...props} aria-invalid={error ? true : props['aria-invalid']}
    aria-describedby={[props['aria-describedby'], error && errorId].filter(Boolean).join(' ') || undefined}>
    <legend>{label}</legend>
    {options.map((option, index) => <label key={option.value} htmlFor={`${id}-${index}`}>
      <input id={`${id}-${index}`} type="radio" name={groupName} value={option.value}
        disabled={option.disabled} checked={value === option.value} onChange={() => onChange(option.value)}/>
      {option.label}</label>)}
    {error && <span id={errorId} role="alert">{error}</span>}
  </fieldset>;
}
