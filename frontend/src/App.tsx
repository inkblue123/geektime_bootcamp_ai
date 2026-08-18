import { useState } from 'react'
import { DatabasePanel } from './DatabasePanel'
import { QueryPanel } from './QueryPanel'
import { Database, Code2 } from 'lucide-react'

function App() {
  const [activeTab, setActiveTab] = useState<'databases' | 'queries'>('databases')

  return (
    <div className="min-h-screen bg-gray-100">
      <header className="bg-white shadow-md">
        <div className="container mx-auto px-4 py-4">
          <div className="flex items-center gap-3">
            <Code2 size={32} className="text-blue-500" />
            <h1 className="text-2xl font-bold">Database Query Tool</h1>
          </div>
        </div>
      </header>

      <div className="container mx-auto px-4 py-6">
        <div className="bg-white rounded-lg shadow-lg overflow-hidden">
          <div className="flex border-b">
            <button
              onClick={() => setActiveTab('databases')}
              className={`flex items-center gap-2 px-6 py-4 font-medium transition-colors ${
                activeTab === 'databases'
                  ? 'border-b-2 border-blue-500 text-blue-600 bg-blue-50'
                  : 'text-gray-600 hover:text-gray-900'
              }`}
            >
              <Database size={18} />
              Database Connections
            </button>
            <button
              onClick={() => setActiveTab('queries')}
              className={`flex items-center gap-2 px-6 py-4 font-medium transition-colors ${
                activeTab === 'queries'
                  ? 'border-b-2 border-blue-500 text-blue-600 bg-blue-50'
                  : 'text-gray-600 hover:text-gray-900'
              }`}
            >
              <Code2 size={18} />
              Query Execution
            </button>
          </div>

          <div className="p-6">
            {activeTab === 'databases' && <DatabasePanel />}
            {activeTab === 'queries' && <QueryPanel />}
          </div>
        </div>
      </div>

      <footer className="bg-white border-t mt-8">
        <div className="container mx-auto px-4 py-4 text-center text-gray-600">
          Database Query Tool - Powered by AI
        </div>
      </footer>
    </div>
  )
}

export default App