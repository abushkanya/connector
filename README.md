# PostgreSQL Connector

A powerful and flexible PostgreSQL database connector with an intuitive query builder interface. This connector provides a simple way to interact with PostgreSQL databases while maintaining clean and readable code.

## Features

- Easy database connection management
- Intuitive query builder interface
- Support for all basic SQL operations (SELECT, INSERT, UPDATE, DELETE)
- Advanced filtering capabilities
- Pagination support
- Group by operations with aggregations
- Multi-language column support
- JSON configuration support
- Automatic table creation and schema updates
- Database backup and restore functionality

## Installation

### Requirements

```bash
pip install psycopg2
pip install tabulate
```

## Usage

### Basic Connection

```python
from connector import PostgreSQLConnector

# Connect using direct credentials
db = PostgreSQLConnector(
    database="your_db",
    host="localhost",
    port="5432",
    user="postgres",
    password="postgres"
)

# Or connect using JSON configuration
db = PostgreSQLConnector(config_json="config.json")
```

### JSON Configuration Example

```json
{
    "database": "your_db",
    "host": "localhost",
    "port": "5432",
    "user": "postgres",
    "password": "postgres",
    "tables": [
        {
            "name": "users",
            "columns": [
                {
                    "name": "id",
                    "type": "SERIAL",
                    "is_primary": true,
                    "not_null": true
                },
                {
                    "name": "username",
                    "type": "VARCHAR(100)",
                    "unique": true,
                    "not_null": true
                },
                {
                    "name": "description",
                    "type": "TEXT",
                    "langs": true
                }
            ]
        }
    ]
}
```

### Query Examples

#### Select Operations

```python
# Get all records
users = db.users.all()

# Get single record by ID
user = db.users.get(id=1)

# Filter with conditions
active_users = db.users.equal(is_active=True).items

# Complex filtering
users = db.users.equal(status="active") \
                .like(username="john") \
                .more(age=18) \
                .items

# Pagination
users = db.users.per_page(10).page(1).items
```

#### Insert Operations

```python
# Add single record
db.users.add(
    username="john_doe",
    email="john@example.com"
).exec()

# Add multiple records
users = db.users
users.add(username="user1", email="user1@example.com")
users.add(username="user2", email="user2@example.com")
users.exec()
```

#### Update Operations

```python
# Update single record
db.users.get(id=1).update(status="inactive").exec()

# Update multiple records
db.users.equal(status="pending") \
        .update(status="active") \
        .exec()
```

#### Delete Operations

```python
# Delete single record
db.users.delete(id=1).exec()

# Delete with conditions
db.users.equal(status="inactive").delete().exec()
```

#### Group By Operations

```python
# Simple group by
result = db.users.group_by("status") \
                 .count("id") \
                 .exec()

# Group by with multiple aggregations
result = db.users.group_by("department") \
                 .count("id") \
                 .summ("salary") \
                 .exec()
```

### Advanced Features

#### Multi-language Support

When a column is defined with `"langs": true` in the JSON configuration, the connector automatically creates additional columns for each supported language (default: 'ru', 'en', 'cn').

```python
# Update multi-language field
db.users.get(id=1).update(
    description_en="English description",
    description_ru="Russian description",
    description_cn="Chinese description"
).exec()
```

#### Backup and Restore

```python
# Create backup of current database
db._backup()

# Restore database from backup
other_db = PostgreSQLConnector(database="other_db")
db._restore(other_db, backup_filename)

# Sync databases
db._sync(other_db)
```

## Error Handling

The connector includes automatic reconnection capabilities and transaction management. Failed operations are automatically rolled back to maintain data integrity.

```python
try:
    result = db.users.add(
        username="john_doe",
        email="invalid_email"
    ).exec()
except Exception as e:
    print(f"Error: {e}")
```

## Contributing

Feel free to submit issues and enhancement requests!

## License

This project is licensed under the MIT License - see the LICENSE file for details.
