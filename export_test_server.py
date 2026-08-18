#!/usr/bin/env python3
"""
Standalone Export Test Server
模拟导出功能的服务器，用于测试导出逻辑
"""
import http.server
import socketserver
import json
import csv
import io
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import os

class ExportTestServer(http.server.SimpleHTTPRequestHandler):
    """测试导出功能的HTTP服务器"""

    def do_GET(self):
        """处理GET请求"""
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {
                "status": "healthy",
                "message": "Export Test Server is running",
                "features": [
                    "CSV Export",
                    "JSON Export",
                    "Execute and Export",
                    "History Export"
                ]
            }
            self.wfile.write(json.dumps(response, indent=2).encode())

        elif self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Export Functionality Test</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }
        .section { margin: 20px 0; padding: 15px; border: 1px solid #ddd; border-radius: 5px; }
        button { padding: 10px 20px; margin: 5px; border: none; border-radius: 5px; cursor: pointer; }
        .btn-csv { background: #4CAF50; color: white; }
        .btn-json { background: #2196F3; color: white; }
        .btn-test { background: #FF9800; color: white; }
        .success { color: green; }
        .result { background: #f5f5f5; padding: 10px; margin: 10px 0; border-radius: 3px; }
    </style>
</head>
<body>
    <h1>🚀 Export Functionality Test</h1>

    <div class="section">
        <h2>📊 功能测试</h2>
        <button onclick="testHealth()" class="btn-test">健康检查</button>
        <button onclick="testExecute()" class="btn-test">执行查询</button>
        <div id="health-result" class="result"></div>
    </div>

    <div class="section">
        <h2>📁 导出测试</h2>
        <button onclick="exportCSV()" class="btn-csv">导出 CSV</button>
        <button onclick="exportJSON()" class="btn-json">导出 JSON</button>
        <button onclick="executeAndExport()" class="btn-test">执行并导出</button>
        <div id="export-result" class="result"></div>
    </div>

    <div class="section">
        <h2>📋 测试数据</h2>
        <pre>
测试查询: SELECT * FROM employees
结果行数: 5行
列: id, name, age, department, salary
        </pre>
    </div>

    <script>
        function testHealth() {
            fetch('/health')
                .then(response => response.json())
                .then(data => {
                    document.getElementById('health-result').innerHTML =
                        '<span class="success">✅ ' + data.message + '</span><br>' +
                        '功能: ' + data.features.join(', ');
                })
                .catch(error => {
                    document.getElementById('health-result').innerHTML = '❌ 错误: ' + error;
                });
        }

        function testExecute() {
            document.getElementById('export-result').innerHTML =
                '<span class="success">✅ 查询执行成功!</span><br>' +
                '执行时间: 45ms<br>' +
                '返回行数: 5行<br>' +
                '状态: success';
        }

        function exportCSV() {
            const csvData = `id,name,age,department,salary
1,Alice,25,Engineering,75000.5
2,Bob,30,Marketing,65000.0
3,Charlie,28,Engineering,80000.0
4,Diana,35,Sales,72000.75
5,Eve,27,Engineering,78000.25`;

            const blob = new Blob([csvData], { type: 'text/csv' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'query_results_' + Date.now() + '.csv';
            a.click();
            URL.revokeObjectURL(url);

            document.getElementById('export-result').innerHTML =
                '<span class="success">✅ CSV 导出成功!</span><br>' +
                '文件名: ' + a.download + '<br>' +
                '大小: ' + csvData.length + ' 字节';
        }

        function exportJSON() {
            const jsonData = {
                query: "SELECT * FROM employees WHERE department = 'Engineering'",
                exported_at: new Date().toISOString(),
                row_count: 5,
                data: [
                    {id: 1, name: "Alice", age: 25, department: "Engineering", salary: 75000.5},
                    {id: 2, name: "Bob", age: 30, department: "Marketing", salary: 65000.0},
                    {id: 3, name: "Charlie", age: 28, department: "Engineering", salary: 80000.0},
                    {id: 4, name: "Diana", age: 35, department: "Sales", salary: 72000.75},
                    {id: 5, name: "Eve", age: 27, department: "Engineering", salary: 78000.25}
                ]
            };

            const blob = new Blob([JSON.stringify(jsonData, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'query_results_' + Date.now() + '.json';
            a.click();
            URL.revokeObjectURL(url);

            document.getElementById('export-result').innerHTML =
                '<span class="success">✅ JSON 导出成功!</span><br>' +
                '文件名: ' + a.download + '<br>' +
                '大小: ' + blob.size + ' 字节';
        }

        function executeAndExport() {
            // 先执行查询
            document.getElementById('export-result').innerHTML =
                '<span class="success">✅ 查询执行成功!</span><br>' +
                '执行时间: 42ms<br>' +
                '返回行数: 5行<br><br>';

            // 然后自动导出
            setTimeout(() => {
                exportCSV();
                const currentResult = document.getElementById('export-result').innerHTML;
                document.getElementById('export-result').innerHTML = currentResult +
                    '<span class="success">✅ 自动导出完成!</span><br>' +
                    '格式: CSV<br>' +
                    '一键操作成功!';
            }, 500);
        }

        // 页面加载时自动检查健康状态
        window.onload = testHealth;
    </script>
</body>
</html>
"""
            self.wfile.write(html_content.encode())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def do_POST(self):
        """处理POST请求"""
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)

        if '/export' in self.path:
            try:
                request_data = json.loads(post_data.decode('utf-8'))
                export_format = request_data.get('format', 'csv').lower()
                query_id = request_data.get('query_history_id', 1)

                # 创建测试数据
                test_data = [
                    {"id": 1, "name": "Alice", "age": 25, "department": "Engineering", "salary": 75000.5},
                    {"id": 2, "name": "Bob", "age": 30, "department": "Marketing", "salary": 65000.0},
                    {"id": 3, "name": "Charlie", "age": 28, "department": "Engineering", "salary": 80000.0},
                    {"id": 4, "name": "Diana", "age": 35, "department": "Sales", "salary": 72000.75},
                    {"id": 5, "name": "Eve", "age": 27, "department": "Engineering", "salary": 78000.25}
                ]

                if export_format == 'csv':
                    # CSV 格式
                    output = io.StringIO()
                    writer = csv.writer(output)
                    headers = list(test_data[0].keys())
                    writer.writerow(headers)
                    for row in test_data:
                        writer.writerow([str(row.get(header, "")) for header in headers])

                    output.seek(0)
                    csv_content = output.getvalue()

                    self.send_response(200)
                    self.send_header('Content-type', 'text/csv')
                    filename = f"query_results_{query_id}_{int(time.time())}.csv"
                    self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                    self.end_headers()
                    self.wfile.write(csv_content.encode('utf-8'))

                elif export_format == 'json':
                    # JSON 格式
                    json_data = {
                        "query": "SELECT * FROM employees WHERE department = 'Engineering'",
                        "exported_at": datetime.now().isoformat(),
                        "row_count": len(test_data),
                        "data": test_data
                    }

                    json_content = json.dumps(json_data, indent=2, ensure_ascii=False)

                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    filename = f"query_results_{query_id}_{int(time.time())}.json"
                    self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                    self.end_headers()
                    self.wfile.write(json_content.encode('utf-8'))

                else:
                    self.send_response(400)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    error_response = {"error": "Unsupported format. Use 'csv' or 'json'"}
                    self.wfile.write(json.dumps(error_response).encode('utf-8'))

            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                error_response = {"error": str(e)}
                self.wfile.write(json.dumps(error_response).encode('utf-8'))

        elif '/execute-and-export' in self.path:
            try:
                request_data = json.loads(post_data.decode('utf-8'))
                export_format = request_data.get('export_format', 'csv').lower()

                # 模拟查询执行
                execution_time = 42  # 模拟执行时间
                test_data = [
                    {"id": 1, "name": "Alice", "age": 25, "department": "Engineering", "salary": 75000.5},
                    {"id": 2, "name": "Bob", "age": 30, "department": "Marketing", "salary": 65000.0},
                    {"id": 3, "name": "Charlie", "age": 28, "department": "Engineering", "salary": 80000.0},
                    {"id": 4, "name": "Diana", "age": 35, "department": "Sales", "salary": 72000.75},
                    {"id": 5, "name": "Eve", "age": 27, "department": "Engineering", "salary": 78000.25}
                ]

                # 返回查询结果和导出信息
                response = {
                    "id": 1,
                    "database_id": request_data.get('database_id', 1),
                    "natural_language_query": request_data.get('natural_language_query', ''),
                    "sql_query": "SELECT * FROM employees WHERE department = 'Engineering'",
                    "execution_time_ms": execution_time,
                    "row_count": len(test_data),
                    "status": "success",
                    "created_at": datetime.now().isoformat(),
                    "results": test_data,
                    "export_info": {
                        "format": export_format,
                        "exported_at": datetime.now().isoformat(),
                        "message": f"Results exported as {export_format.upper()}"
                    }
                }

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response, indent=2).encode('utf-8'))

            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                error_response = {"error": str(e)}
                self.wfile.write(json.dumps(error_response).encode('utf-8'))

        else:
            self.send_response(404)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            error_response = {"error": "Endpoint not found"}
            self.wfile.write(json.dumps(error_response).encode('utf-8'))

    def log_message(self, format, *args):
        """自定义日志消息"""
        print(f"[{self.log_date_time_string()}] {format % args}")

def run_server(port=8888):
    """启动测试服务器"""
    print(f"🚀 Export Test Server starting on port {port}")
    print(f"🌐 Open http://localhost:{port} in your browser")
    print(f"📡 API endpoints available:")
    print(f"   - GET  /health")
    print(f"   - POST /export")
    print(f"   - POST /execute-and-export")
    print("=" * 60)

    try:
        with socketserver.TCPServer(("", port), ExportTestServer) as httpd:
            print(f"✅ Server is running. Press Ctrl+C to stop.")
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopped.")
    except OSError as e:
        print(f"❌ Error starting server: {e}")
        print(f"💡 Try a different port: python3 export_test_server.py --port 8889")

if __name__ == "__main__":
    import sys

    port = 8888
    if len(sys.argv) > 1:
        if sys.argv[1] == "--port" and len(sys.argv) > 2:
            port = int(sys.argv[2])

    run_server(port)