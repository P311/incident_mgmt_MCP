import Header from './components/Header'
import DimensionValues from './components/DimensionValues'
import DurationPanel from './components/DurationPanel'
import CounterPanel from './components/CounterPanel'
import ReportPanel from './components/ReportPanel'
import useDimensionOptions from './useDimensionOptions'
import './App.css'

export default function App() {
  const { options } = useDimensionOptions()

  return (
    <>
      <Header />
      <main>
        <DimensionValues />
        <DurationPanel options={options} />
        <CounterPanel options={options} />
        <ReportPanel />
      </main>
    </>
  )
}
