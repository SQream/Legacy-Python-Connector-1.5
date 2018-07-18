# Python2.7/3.3+ connector for SQream DB

#### This is a deprecated version. Please use the new version - 

https://github.com/SQream/SQream-Python-Connector

Tested on SQream protocols 4 and 5

Usage example:

Import the `pysqream.py` file into your own codebase::

```python
import pysqream as sq
import atexit
from datetime import date, datetime

sc = sq.Connector() # Create new connection instance

atexit.register(sc.close) # Optional but recommended - close connection upon exit

sc.connect(host='<ip>', database='<database>', user='<username>', password='<password>',
           port=<server port>, clustered=<True/False - based on your installation>, timeout=<socket timeout>)
            
qr = sc.query("SELECT x,y FROM t") # Run query and get results
print(sc.cols_names()) # Print column names
print(sc.cols_types()) # Print column types
print(sc.cols_to_rows()) # Print results, as a list of tuples
sc.close() # Close connection
```
