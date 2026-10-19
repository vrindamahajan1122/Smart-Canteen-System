Smart Campus Canteen – Pre-Ordering & Queue Management System

About the Project

Smart Campus Canteen is a web-based application developed using Flask and MySQL to simplify canteen ordering and reduce waiting time for students. Students can view available food items, place orders in advance, and track their order status. The system helps canteen staff manage orders efficiently through a kitchen dashboard and live order display.

The project aims to provide a convenient, organized, and time-efficient canteen ordering experience within a college campus.

Features

- Online Pre-Ordering: Browse the menu and place food orders in advance.
- Shopping Cart: Select food items and calculate the total order price.
- Order Token: Receive an order token after placing an order.
- Live Order Status: Track order progress through different stages.
- Kitchen Dashboard: Allow canteen staff to view and update order statuses.
- Live Display Board: Show current orders and their preparation status.
- Queue Management: Help students and staff manage order flow efficiently.
- MySQL Database: Store menu items and order information.

Additional features such as estimated waiting time, QR-based order tracking, pickup slot management, analytics, feedback, order cancellation, and dish customization can be documented here once implemented and tested.

Technology Stack

- Frontend: HTML, CSS, JavaScript
- Backend: Python, Flask
- Database: MySQL
- Database Connectivity: PyMySQL
- Development Tools: Visual Studio Code, MySQL Workbench
- Version Control: Git and GitHub

Project Structure

Smart-Campus-Canteen/
│
├── app.py
├── schema.sql
├── requirements.txt
├── README.md
├── .gitignore
│
└── templates/
    ├── index.html
    ├── kitchen.html
    └── display.html

The structure above represents the existing core files. Add any additional files or templates that your implementation uses.

Prerequisites

Make sure the following are installed:

- Python 3
- MySQL Server
- MySQL Workbench (recommended)
- Git

Installation and Setup

1. Clone the Repository

git clone YOUR_GITHUB_REPOSITORY_URL
cd YOUR_PROJECT_FOLDER

Replace the repository URL and folder name with your actual values.

2. Create a Virtual Environment (Recommended)

python -m venv venv

Activate it on Windows:

venv\Scripts\activate

3. Install Dependencies

pip install -r requirements.txt

4. Set Up the MySQL Database

1. Open MySQL Workbench.
2. Connect to your MySQL server.
3. Open a new SQL query tab.
4. Copy and execute the database creation and table setup SQL provided for the project.
5. Confirm that the database and required tables have been created.

If using "schema.sql", you can open the file in MySQL Workbench and execute its SQL statements.

5. Configure the Database Connection

Open "app.py" and configure the MySQL connection settings to match your local MySQL setup.

Do not upload database passwords or other secret credentials to GitHub. Use environment variables for sensitive configuration where possible.

6. Run the Application

python app.py

Open the local URL shown in the terminal, usually:

http://127.0.0.1:5000

The application must be running for the pages and API endpoints to work.

Application Pages

Page| Purpose
Student Menu| Browse food items and place orders
Kitchen Dashboard| View orders and update their statuses
Live Display Board| Display current order information

Database

The core database stores information about:

- Menu Items: Food item names, prices, and availability.
- Orders: Student details, selected items, total price, pickup time, order status, and creation time.

The database schema may be extended as new features are implemented.

Future Enhancements

- Live estimated preparation time and queue position
- QR-based order tracking
- Pickup slot capacity management
- Sales and order analytics dashboard
- Food demand prediction
- Student feedback and ratings
- Order cancellation and payment status management
- Dish-specific food customization

Project Purpose

This project demonstrates the practical use of web development, database management, backend programming, and order workflow management to solve a real-world problem in a college canteen.

License

This project is intended for educational and academic purposes. Add a license if you decide to distribute it under specific terms.
