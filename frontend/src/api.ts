import { useState } from 'react'
import axios from 'axios'

const API_BASE = '/api/v1'

export interface DatabaseConnection {
  id: number
  name: string
  connection_string: string
  description: string
  created_at: string
  updated_at: string
}

export interface QueryHistory {
  id: number
  database_id: number
  natural_language_query: string
  sql_query: string
  execution_time_ms: number
  row_count: number
  status: string
  created_at: string
  results: any[]
}

export interface TableInfo {
  name: string
  type: string
  columns: Array<{
    name: string
    type: string
    not_null: boolean
    primary_key: boolean
  }>
}

export interface ExportRequest {
  query_history_id: number
  format: 'csv' | 'json'
}

export interface ExecuteAndExportRequest {
  database_id: number
  natural_language_query?: string
  sql_query?: string
  export_format: 'csv' | 'json'
}

export function useDatabases() {
  const [databases, setDatabases] = useState<DatabaseConnection[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchDatabases = async () => {
    try {
      setLoading(true)
      const response = await axios.get(`${API_BASE}/database`)
      setDatabases(response.data)
      setError(null)
    } catch (err) {
      setError('Failed to fetch databases')
      console.error(err)
    } finally {
      setLoading(false)
    }
  }

  const createDatabase = async (data: {
    name: string
    connection_string: string
    description: string
  }) => {
    try {
      await axios.post(`${API_BASE}/database`, data)
      await fetchDatabases()
    } catch (err) {
      setError('Failed to create database')
      throw err
    }
  }

  const deleteDatabase = async (id: number) => {
    try {
      await axios.delete(`${API_BASE}/database/${id}`)
      await fetchDatabases()
    } catch (err) {
      setError('Failed to delete database')
      throw err
    }
  }

  return {
    databases,
    loading,
    error,
    fetchDatabases,
    createDatabase,
    deleteDatabase
  }
}

export function useQueries() {
  const [history, setHistory] = useState<QueryHistory[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const executeQuery = async (request: {
    database_id: number
    natural_language_query?: string
    sql_query?: string
  }) => {
    try {
      setLoading(true)
      const response = await axios.post(`${API_BASE}/query`, request)
      setError(null)
      return response.data
    } catch (err) {
      setError('Failed to execute query')
      throw err
    } finally {
      setLoading(false)
    }
  }

  const executeAndExportQuery = async (request: ExecuteAndExportRequest) => {
    try {
      setLoading(true)
      const response = await axios.post(`${API_BASE}/query/execute-and-export`, request)
      setError(null)
      return response.data
    } catch (err) {
      setError('Failed to execute and export query')
      throw err
    } finally {
      setLoading(false)
    }
  }

  const exportQuery = async (request: ExportRequest) => {
    try {
      const response = await axios.post(`${API_BASE}/export`, request, {
        responseType: 'blob'
      })

      // Create download link
      const url = window.URL.createObjectURL(new Blob([response.data]))
      const link = document.createElement('a')
      link.href = url

      // Extract filename from content disposition header
      const contentDisposition = response.headers['content-disposition']
      let filename = `query_results_${Date.now()}.${request.format}`
      if (contentDisposition) {
        const filenameMatch = contentDisposition.match(/filename="?([^"]+)"?/)
        if (filenameMatch && filenameMatch[1]) {
          filename = filenameMatch[1]
        }
      }

      link.setAttribute('download', filename)
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.URL.revokeObjectURL(url)

      setError(null)
      return { success: true, filename }
    } catch (err) {
      setError('Failed to export query results')
      throw err
    }
  }

  const fetchHistory = async (databaseId?: number) => {
    try {
      setLoading(true)
      const params = databaseId ? `?database_id=${databaseId}` : ''
      const response = await axios.get(`${API_BASE}/query/history${params}`)
      setHistory(response.data)
      setError(null)
    } catch (err) {
      setError('Failed to fetch query history')
      console.error(err)
    } finally {
      setLoading(false)
    }
  }

  return {
    history,
    loading,
    error,
    executeQuery,
    executeAndExportQuery,
    exportQuery,
    fetchHistory
  }
}

export function useMetadata() {
  const [metadata, setMetadata] = useState<TableInfo[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchMetadata = async (connectionId: number) => {
    try {
      setLoading(true)
      const response = await axios.get(`${API_BASE}/database/${connectionId}/metadata`)
      setMetadata(response.data)
      setError(null)
    } catch (err) {
      setError('Failed to fetch database metadata')
      console.error(err)
    } finally {
      setLoading(false)
    }
  }

  return {
    metadata,
    loading,
    error,
    fetchMetadata
  }
}

export async function checkHealth() {
  try {
    const response = await axios.get('/health')
    return response.data
  } catch (err) {
    throw new Error('Backend health check failed')
  }
}