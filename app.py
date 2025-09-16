from flask import Flask, render_template, request, redirect, flash, url_for
import smtplib
import os
from dotenv import load_dotenv
from email.message import EmailMessage

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

EMAIL_USER = os.getenv('EMAIL_USER')
EMAIL_PASS = os.getenv('EMAIL_PASS')

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

@app.route('/send_message', methods=['POST'])
def send_message():
    name = request.form['name']
    email = request.form['email']
    phone = request.form['phone']
    subject = request.form['subject']
    message = request.form['message']

    email_content = f"""
    New Contact Form Submission:

    Name: {name}
    Email: {email}
    Phone: {phone}
    Subject: {subject}
    Message:
    {message}
    """

    msg = EmailMessage()
    msg.set_content(email_content)
    msg['Subject'] = f"Loyalty Contact Form - {subject}"
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_USER

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as smtp:
            smtp.starttls()
                # Add these two lines below:
            print("EMAIL_USER =", EMAIL_USER)
            print("EMAIL_PASS =", EMAIL_PASS)
            smtp.login(EMAIL_USER, EMAIL_PASS)
            smtp.send_message(msg)
        flash('Message sent successfully!', 'success')
    except Exception as e:
        print(f"Email failed to send: {e}")
        flash('Failed to send message. Please try again later.', 'danger')

    return redirect(url_for('contact'))

if __name__ == '__main__':
    app.run(debug=True)
