import zipfile
import os

# Define the content of our test files
files_to_create = {
    "ProductList.jsx": """
import React, { useState, useEffect } from 'react';

const ProductList = () => {
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    useEffect(() => {
        // Trigger: API_IN_COMPONENT
        fetch('https://api.example.com/products')
            .then(res => res.json())
            .then(data => {
                setItems(data);
                setLoading(false);
            })
            .catch(err => setError(err));
    }, []);

    // Trigger: DUPLICATE_FUNCTION (Shared with Cart.jsx)
    const formatCurrency = (value) => {
        return '$' + value.toFixed(2);
    };

    return <div>{items.length} Products</div>;
};

export default ProductList;
""",
    "Cart.jsx": """
import React, { useState } from 'react';

const Cart = () => {
    const [cart, setCart] = useState([]);

    // Trigger: DUPLICATE_FUNCTION (Exact same logic as ProductList)
    const formatCurrency = (value) => {
        return '$' + value.toFixed(2);
    };

    return <div>Total: {formatCurrency(100)}</div>;
};
""",
    "UserStats.js": """
const getApiData = async () => {
    // Trigger: API_IN_COMPONENT (via axios)
    const response = await axios.get('/user/profile');
    return response.data;
};
"""
}

# Create the ZIP file
zip_name = "test_cases.zip"
with zipfile.ZipFile(zip_name, 'w') as z:
    for filename, content in files_to_create.items():
        z.writestr(filename, content.strip())

print(f"Successfully created {zip_name} in your current folder!")