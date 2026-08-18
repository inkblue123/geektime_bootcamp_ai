import { useState, useEffect } from 'react'
import { useQueries, useDatabases, checkHealth, ExportRequest } from './api'
import { Play, Clock, AlertCircle, Download, FileDown } from 'lucide-react'

export function QueryPanel() {
  const { databases, fetchDatabases } = useDatabases()
  const { executeQuery, executeAndExportQuery, exportQuery, fetchHistory, history, loading } = useQueries()
  const [selectedDatabase, setSelectedDatabase] = useState<number | null>(null)
  const [naturalLanguage, setNaturalLanguage] = useState('')
  const [sqlQuery, setSqlQuery] = useState('')
  const [results, setResults] = useState<any[] | null>(null)
  const [executionTime, setExecutionTime] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [backendStatus, setBackendStatus] = useState<'checking' | 'healthy' | 'error'>('checking')
  const [currentQueryId, setCurrentQueryId] = useState<number | null>(null)
  const [exportFormat, setExportFormat] = useState<'csv' | 'json'>('csv')
  const [exporting, setExporting] = useState(false)
  const [exportMessage, setExportMessage] = useState<string | null>(null)

  useEffect(() => {
    const checkBackendHealth = async () => {
      try {
        await checkHealth()
        setBackendStatus('healthy')
        fetchDatabases()
      } catch (err) {
        setBackendStatus('error')
      }
    }
    checkBackendHealth()
    fetchHistory()
  }, [])

  const handleExecute = async () => {
    if (!selectedDatabase) {
      setError('Please select a database')
      return
    }

    if (!naturalLanguage && !sqlQuery) {
      setError('Please enter either a natural language query or SQL query')
      return
    }

    try {
      setError(null)
      const response = await executeQuery({
        database_id: selectedDatabase,
        natural_language_query: naturalLanguage || undefined,
        sql_query: sqlQuery || undefined
      })

      setResults(response.results)
      setExecutionTime(response.execution_time_ms)
      setSqlQuery(response.sql_query)
      setCurrentQueryId(response.id)
      fetchHistory(selectedDatabase)

      if (response.status === 'error') {
        setError(response.error_message || 'Query execution failed')
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to execute query')
      console.error(err)
    }
  }

  const handleExport = async () => {
    if (!currentQueryId || !results || results.length === 0) {
      setError('No query results to export')
      return
    }

    try {
      setExporting(true)
      setError(null)
      await exportQuery({
        query_history_id: currentQueryId,
        format: exportFormat
      })
      setExportMessage(`Results exported as ${exportFormat.toUpperCase()} successfully!`)
      setTimeout(() => setExportMessage(null), 3000)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to export results')
      console.error(err)
    } finally {
      setExporting(false)
    }
  }

  const handleExecuteAndExport = async () => {
    if (!selectedDatabase) {
      setError('Please select a database')
      return
    }

    if (!naturalLanguage && !sqlQuery) {
      setError('Please enter either a natural language query or SQL query')
      return
    }

    try {
      setError(null)
      const response = await executeAndExportQuery({
        database_id: selectedDatabase,
        natural_language_query: naturalLanguage || undefined,
        sql_query: sqlQuery || undefined,
        export_format: exportFormat
      })

      setResults(response.results)
      setExecutionTime(response.execution_time_ms)
      setSqlQuery(response.sql_query)
      setCurrentQueryId(response.id)
      fetchHistory(selectedDatabase)

      if (response.status === 'error') {
        setError(response.error_message || 'Query execution failed')
      } else {
        // Trigger file download for the exported file
        if (response.export_info) {
          setExportMessage(`${response.export_info.message} - File downloaded!`)
          setTimeout(() => setExportMessage(null), 3000)
        }
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to execute and export query')
      console.error(err)
    }
  }

  const handleHistoryExport = async (queryId: number) => {
    try {
      setExporting(true)
      setError(null)
      await exportQuery({
        query_history_id: queryId,
        format: exportFormat
      })
      setExportMessage(`History query exported as ${exportFormat.toUpperCase()} successfully!`)
      setTimeout(() => setExportMessage(null), 3000)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to export history query')
      console.error(err)
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="p-6">
      <div className="mb-6">
        <div className="flex items-center gap-2 mb-4">
          <h2 className="text-2xl font-bold">Query Execution</h2>
          {backendStatus === 'healthy' && (
            <span className="px-2 py-1 text-xs bg-green-100 text-green-700 rounded">Connected</span>
          )}
          {backendStatus === 'error' && (
            <span className="flex items-center gap-1 px-2 py-1 text-xs bg-red-100 text-red-700 rounded">
              <AlertCircle size={12} />
              Backend unavailable
            </span>
          )}
        </div>

        <div className="mb-4">
          <label className="block text-sm font-medium mb-2">Select Database</label>
          <select
            value={selectedDatabase || ''}
            onChange={(e) => setSelectedDatabase(e.target.value ? parseInt(e.target.value) : null)}
            className="w-full px-3 py-2 border rounded-lg"
            disabled={backendStatus === 'error'}
          >
            <option value="">-- Select a database --</option>
            {databases.map((db) => (
              <option key={db.id} value={db.id}>{db.name}</option>
            ))}
          </select>
        </div>

        <div className="mb-4">
          <label className="block text-sm font-medium mb-2">Natural Language Query (Optional)</label>
          <textarea
            value={naturalLanguage}
            onChange={(e) => setNaturalLanguage(e.target.value)}
            placeholder="Ask a question about your data in plain English..."
            className="w-full px-3 py-2 border rounded-lg h-24"
            disabled={backendStatus === 'error'}
          />
        </div>

        <div className="mb-4">
          <label className="block text-sm font-medium mb-2">SQL Query (Optional)</label>
          <textarea
            value={sqlQuery}
            onChange={(e) => setSqlQuery(e.target.value)}
            placeholder="SELECT * FROM table_name..."
            className="w-full px-3 py-2 border rounded-lg h-24 font-mono"
            disabled={backendStatus === 'error'}
          />
        </div>

        <div className="mb-4">
          <label className="block text-sm font-medium mb-2">Export Format</label>
          <select
            value={exportFormat}
            onChange={(e) => setExportFormat(e.target.value as 'csv' | 'json')}
            className="px-3 py-2 border rounded-lg"
            disabled={backendStatus === 'error'}
          >
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </select>
        </div>

        <div className="flex gap-3">
          <button
            onClick={handleExecute}
            disabled={loading || !selectedDatabase || (!naturalLanguage && !sqlQuery)}
            className="flex items-center gap-2 px-6 py-3 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Play size={16} />
            Execute Query
          </button>

          <button
            onClick={handleExecuteAndExport}
            disabled={loading || !selectedDatabase || (!naturalLanguage && !sqlQuery)}
            className="flex items-center gap-2 px-6 py-3 bg-green-500 text-white rounded-lg hover:bg-green-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <FileDown size={16} />
            Execute & Export
          </button>

          <button
            onClick={handleExport}
            disabled={exporting || !currentQueryId || !results || results.length === 0}
            className="flex items-center gap-2 px-6 py-3 bg-purple-500 text-white rounded-lg hover:bg-purple-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Download size={16} />
            Export Current
          </button>
        </div>

        {error && (
          <div className="mt-4 p-4 bg-red-100 border border-red-400 text-red-700 rounded flex items-start gap-2">
            <AlertCircle size={16} className="mt-0.5" />
            <div>{error}</div>
          </div>
        )}

        {exportMessage && (
          <div className="mt-4 p-4 bg-green-100 border border-green-400 text-green-700 rounded flex items-start gap-2">
            <Download size={16} className="mt-0.5" />
            <div>{exportMessage}</div>
          </div>
        )}

        {executionTime && !error && !exportMessage && (
          <div className="mt-4 p-4 bg-green-100 border border-green-400 text-green-700 rounded">
            Query executed in {executionTime}ms
          </div>
        )}
      </div>

      {results && results.length > 0 && (
        <div className="mt-6">
          <h3 className="text-xl font-semibold mb-4">Results ({results.length} rows)</h3>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr className="bg-gray-100">
                  {Object.keys(results[0]).map((key) => (
                    <th key={key} className="px-4 py-2 text-left border font-medium">
                      {key}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {results.map((row, idx) => (
                  <tr key={idx} className="border-b hover:bg-gray-50">
                    {Object.values(row).map((value, cellIdx) => (
                      <td key={cellIdx} className="px-4 py-2 border">
                        {String(value)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {history.length > 0 && (
        <div className="mt-8">
          <h3 className="text-xl font-semibold mb-4 flex items-center gap-2">
            <Clock size={20} />
            Query History
          </h3>
          <div className="space-y-2">
            {history.map((query) => (
              <div key={query.id} className="p-3 border rounded hover:bg-gray-50">
                <div className="flex justify-between items-start">
                  <div className="flex-1">
                    {query.natural_language_query && (
                      <p className="text-sm text-gray-600 mb-1">
                        {query.natural_language_query}
                      </p>
                    )}
                    <code className="text-xs bg-gray-100 px-2 py-1 rounded block">
                      {query.sql_query}
                    </code>
                  </div>
                  <div className="flex items-center gap-4">
                    <div className="text-xs text-gray-500 text-right">
                      <div>{query.status}</div>
                      <div>{query.execution_time_ms}ms</div>
                      <div>{query.row_count} rows</div>
                    </div>
                    {query.status === 'success' && query.row_count > 0 && (
                      <button
                        onClick={() => handleHistoryExport(query.id)}
                        className="text-xs px-2 py-1 bg-gray-100 hover:bg-gray-200 rounded flex items-center gap-1"
                        disabled={exporting}
                      >
                        <Download size={12} />
                        Export
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}