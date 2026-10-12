# Airtable Schema Documentation for ShoeSort

## Overview

This document outlines the Airtable schema for the ShoeSort application and provides examples of how to find orders by barcode.

## Configuration

### API Credentials
- **API Key**: `<your-airtable-personal-access-token>`
- **Base ID**: `appF7vOU3fWTMJ5RV` (Production)
- **Test Base ID**: `appUp6ymPx9OOrLoC` (Development/Testing)

### Environment Variables
```bash
AIRTABLE_API_KEY=<your-airtable-personal-access-token>
AIRTABLE_BASE_ID=appF7vOU3fWTMJ5RV
```

## Current Schema

### 1. Partners Table
Based on the code analysis, you currently have a `Partners` table with at least:

**Fields:**
- `Partner Name` (Text) - Name of the partner organization

**Example Usage:**
```python
from shoesort.api import AirtableAPI
from shoesort.config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID

api = AirtableAPI(AIRTABLE_API_KEY, AIRTABLE_BASE_ID)
partners = api.list_records("Partners", max_records=5)
```

## Recommended Extended Schema for Orders

To support order tracking by barcode, I recommend adding these tables:

### 2. Orders Table (Recommended)

**Fields:**
- `Order ID` (Text, Primary) - Unique order identifier
- `Barcode` (Text) - Tracking barcode/number
- `Partner` (Link to Partners) - Reference to partner
- `Status` (Single Select) - pending, processing, completed, shipped
- `Created Date` (Date) - When order was created
- `Items Count` (Number) - Number of items in order
- `Total Value` (Currency) - Total order value
- `Shipping Address` (Long Text) - Customer address
- `Customer Email` (Email) - Customer contact
- `Notes` (Long Text) - Additional information

### 3. Order Items Table (Recommended)

**Fields:**
- `Item ID` (Text, Primary) - Unique item identifier
- `Order` (Link to Orders) - Reference to parent order
- `Shoe ID` (Text) - Reference to detected shoe
- `Brand` (Text) - Shoe brand
- `Model` (Text) - Shoe model
- `Size` (Text) - Shoe size
- `Color` (Text) - Shoe color
- `Price` (Currency) - Item price
- `Image URL` (URL) - Link to shoe image
- `Processing Status` (Single Select) - detected, verified, processed

## Barcode Search Methods

### Method 1: Search Orders by Barcode (Airtable)

```python
from shoesort.api import AirtableAPI
from shoesort.config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID

def find_order_by_barcode(barcode: str):
    """Find order in Airtable by barcode."""
    api = AirtableAPI(AIRTABLE_API_KEY, AIRTABLE_BASE_ID)
    
    # Search using Airtable's filter formula
    url = f"{api.base_url}/Orders"
    params = {
        "filterByFormula": f"{{Barcode}} = '{barcode}'",
        "maxRecords": 100
    }
    
    response = requests.get(url, headers=api.headers, params=params)
    response.raise_for_status()
    
    records = response.json().get('records', [])
    return records

# Usage
order = find_order_by_barcode("12345678")
if order:
    print(f"Found order: {order[0]['fields']['Order ID']}")
```

### Method 2: Search Local Database by Barcode (Current Implementation)

Your system already supports barcode search in the local PostgreSQL database:

```python
from shoesort.database import get_shoes_by_barcode, get_processing_runs_by_barcode

def find_shoes_by_barcode(barcode: str):
    """Find shoes in local DB by barcode."""
    # Get processing runs
    runs = get_processing_runs_by_barcode(barcode)
    
    # Get shoes
    shoes = get_shoes_by_barcode(barcode)
    
    return {
        'barcode': barcode,
        'total_runs': len(runs),
        'total_shoes': len(shoes),
        'runs': runs,
        'shoes': shoes
    }

# Usage
results = find_shoes_by_barcode("TEST123456")
```

### Method 3: Enhanced AirtableAPI with Barcode Search

Let me extend your existing AirtableAPI class:

```python
class EnhancedAirtableAPI(AirtableAPI):
    def search_by_field(self, table_name: str, field_name: str, value: str) -> List[Dict]:
        """Search records by a specific field value."""
        url = f"{self.base_url}/{table_name}"
        params = {
            "filterByFormula": f"{{{field_name}}} = '{value}'",
            "maxRecords": 100
        }
        
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json().get('records', [])
    
    def find_order_by_barcode(self, barcode: str) -> List[Dict]:
        """Find orders by barcode."""
        return self.search_by_field("Orders", "Barcode", barcode)
    
    def find_items_by_order_id(self, order_id: str) -> List[Dict]:
        """Find items belonging to an order."""
        return self.search_by_field("Order Items", "Order ID", order_id)
    
    def get_order_with_items(self, barcode: str) -> Dict:
        """Get complete order information including items."""
        orders = self.find_order_by_barcode(barcode)
        
        if not orders:
            return None
            
        order = orders[0]
        order_id = order['fields']['Order ID']
        items = self.find_items_by_order_id(order_id)
        
        return {
            'order': order,
            'items': items,
            'total_items': len(items)
        }
```

## API Endpoints for Barcode Search

### Local Database Endpoints (Already Implemented)

1. **Get shoes by barcode:**
   ```
   GET /api/shoes/by-barcode/<barcode>
   ```

2. **Barcode search page:**
   ```
   GET /barcode-search?barcode=<barcode>
   ```

### Recommended Airtable Integration Endpoints

```python
@app.route('/api/airtable/orders/by-barcode/<barcode>')
def api_airtable_order_by_barcode(barcode):
    """Find order in Airtable by barcode."""
    try:
        api = EnhancedAirtableAPI(AIRTABLE_API_KEY, AIRTABLE_BASE_ID)
        result = api.get_order_with_items(barcode)
        
        if not result:
            return jsonify({'error': 'Order not found'}), 404
            
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/sync-order-to-airtable/<barcode>')
def sync_order_to_airtable(barcode):
    """Sync detected shoes to Airtable order."""
    try:
        # Get local data
        local_data = find_shoes_by_barcode(barcode)
        
        # Find or create order in Airtable
        api = EnhancedAirtableAPI(AIRTABLE_API_KEY, AIRTABLE_BASE_ID)
        orders = api.find_order_by_barcode(barcode)
        
        if not orders:
            # Create new order
            order_data = {
                "Order ID": f"ORD-{barcode}",
                "Barcode": barcode,
                "Status": "processing",
                "Created Date": datetime.now().isoformat(),
                "Items Count": local_data['total_shoes']
            }
            order = api.create_record("Orders", order_data)
        else:
            order = orders[0]
            
        # Create/update items
        for shoe in local_data['shoes']:
            item_data = {
                "Item ID": str(shoe.unique_id),
                "Order ID": order['fields']['Order ID'],
                "Brand": shoe.brand or "Unknown",
                "Model": shoe.model or "Unknown",
                "Price": shoe.price or "Unknown",
                "Processing Status": "detected"
            }
            api.create_record("Order Items", item_data)
            
        return jsonify({'success': True, 'order': order})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
```

## Usage Examples

### 1. Basic Search
```python
from shoesort.api import AirtableAPI

api = AirtableAPI(api_key, base_id)

# List all partners
partners = api.list_records("Partners")

# Get specific order
order = api.get_record("Orders", "rec123456")

# Create new order
new_order = api.create_record("Orders", {
    "Order ID": "ORD-001",
    "Barcode": "12345678",
    "Status": "pending"
})
```

### 2. Complete Barcode Search Workflow
```python
def complete_barcode_search(barcode: str):
    """Complete search across both local DB and Airtable."""
    
    # 1. Search local database
    local_results = find_shoes_by_barcode(barcode)
    
    # 2. Search Airtable
    api = EnhancedAirtableAPI(AIRTABLE_API_KEY, AIRTABLE_BASE_ID)
    airtable_results = api.get_order_with_items(barcode)
    
    # 3. Combine results
    return {
        'barcode': barcode,
        'local_database': local_results,
        'airtable': airtable_results,
        'found_in_local': local_results['total_shoes'] > 0,
        'found_in_airtable': airtable_results is not None
    }
```

## Best Practices

1. **Use Environment Variables**: Always store API keys in `.env` file
2. **Error Handling**: Wrap API calls in try-catch blocks
3. **Rate Limiting**: Airtable has rate limits (5 requests/second)
4. **Caching**: Cache frequent lookups to improve performance
5. **Validation**: Validate barcode format before searching
6. **Sync Strategy**: Decide when to sync between local DB and Airtable

## Security Considerations

1. **API Key Protection**: Never commit API keys to version control
2. **Access Control**: Use Airtable's permission system
3. **Input Validation**: Sanitize all user inputs
4. **HTTPS**: Always use HTTPS for API calls
5. **Logging**: Log API usage but not sensitive data 