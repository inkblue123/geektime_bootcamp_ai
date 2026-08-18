import { useState } from 'react'
import { useDatabases, DatabaseConnection } from './api'
import { Trash2, Plus } from 'lucide-react'

export function DatabasePanel() {
  const {
    databases,
    loading,
    error,
    fetchDatabases,
    createDatabase,
    deleteDatabase
  } = useDatabases()

  const [showForm, setShowForm] = useState(false)
  const [formData, setFormData] = useState({
    name: '',
    connection_string: '',
    description: ''
  })

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await createDatabase(formData)
      setShowForm(false)
      setFormData({ name: '', connection_string: '', description: '' })
    } catch (err) {
      console.error('Failed to create database:', err)
    }
  }

  const handleDelete = async (id: number) => {
    if (window.confirm('Are you sure you want to delete this database connection?')) {
      try {
        await deleteDatabase(id)
      } catch (err) {
        console.error('Failed to delete database:', err)
      }
    }
  }

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <h2 className="text-2xl font-bold">Database Connections</h2>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600"
        >
          <Plus size={16} />
          Add Connection
        </button>
      </div>

      {error && (
        <div className="mb-4 p-4 bg-red-100 border border-red-400 text-red-700 rounded">
          {error}
        </div>
      )}

      {showForm && (
        <form onSubmit={handleSubmit} className="mb-6 p-4 border rounded-lg bg-gray-50">
          <div className="mb-4">
            <label className="block text-sm font-medium mb-2">Name</label>
            <input
              type="text"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              className="w-full px-3 py-2 border rounded-lg"
              required
            />
          </div>
          <div className="mb-4">
            <label className="block text-sm font-medium mb-2">Connection String</label>
            <input
              type="text"
              value={formData.connection_string}
              onChange={(e) => setFormData({ ...formData, connection_string: e.target.value })}
              placeholder="sqlite:///path/to/database.db"
              className="w-full px-3 py-2 border rounded-lg"
              required
            />
          </div>
          <div className="mb-4">
            <label className="block text-sm font-medium mb-2">Description</label>
            <input
              type="text"
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              className="w-full px-3 py-2 border rounded-lg"
            />
          </div>
          <div className="flex gap-2">
            <button
              type="submit"
              className="px-4 py-2 bg-green-500 text-white rounded-lg hover:bg-green-600"
            >
              Save Connection
            </button>
            <button
              type="button"
              onClick={() => setShowForm(false)}
              className="px-4 py-2 bg-gray-300 rounded-lg hover:bg-gray-400"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {loading ? (
        <div className="text-center py-4">Loading...</div>
      ) : databases.length === 0 ? (
        <div className="text-center py-8 text-gray-500">
          No database connections found. Add your first connection to get started.
        </div>
      ) : (
        <div className="grid gap-4">
          {databases.map((db: DatabaseConnection) => (
            <div key={db.id} className="p-4 border rounded-lg hover:shadow-md transition-shadow">
              <div className="flex justify-between items-start">
                <div>
                  <h3 className="font-semibold text-lg">{db.name}</h3>
                  <p className="text-sm text-gray-600 mt-1">{db.description || 'No description'}</p>
                  <code className="text-xs bg-gray-100 px-2 py-1 rounded mt-2 block">
                    {db.connection_string}
                  </code>
                </div>
                <button
                  onClick={() => handleDelete(db.id)}
                  className="p-2 text-red-500 hover:bg-red-50 rounded"
                  title="Delete connection"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}