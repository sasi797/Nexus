pipeline {
    agent any
    stages {
        stage('Checkout') {
            steps { checkout scm }
        }
        stage('Inject .env') {
            steps { sh 'cp /home/jenkins/.env .env' }
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
                sh 'sleep 5 && curl -sfkL https://nexus-api.linkworks.in/docs > /dev/null && echo "API is up"'
            }
        }
    }
}
