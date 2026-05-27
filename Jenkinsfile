pipeline {
    agent any
    stages {
        stage('Checkout') {
            steps { checkout scm }
        }
        stage('Inject .env') {
            steps { sh 'cp /home/ubuntu/.env .env' }
        }
        stage('Deploy') {
            steps {
                sh 'docker compose down || true'
                sh 'docker compose build'
                sh 'docker compose up -d'
            }
        }
        stage('Health Check') {
            steps {
                sh 'sleep 5 && curl -sf http://localhost/docs > /dev/null && echo "API is up"'
            }
        }
    }
}
