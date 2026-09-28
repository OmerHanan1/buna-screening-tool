@description('Existing dedicated private registry login server.')
param registryServer string
@description('Immutable image digest, not a floating tag.')
param image string
param pullIdentityId string
param environmentName string = 'paper-overlap-env'
param appName string = 'paper-overlap-api'
param location string = resourceGroup().location
param frontendOrigin string = 'https://omerhanan1.github.io'
@description('Leave false until actual Azure worker isolation and synthetic workflow are verified.')
param publicIngress bool = false
@allowed([0, 1])
@description('Use 1 only during bounded private runtime validation; restore 0 before release or when blocked.')
param minimumReplicas int = 0

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource application 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${pullIdentityId}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [{ server: registryServer, identity: pullIdentityId }]
      ingress: {
        external: publicIngress
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
    }
    template: {
      containers: [{
        name: 'api'
        image: image
        resources: { cpu: 1, memory: '2Gi' }
        env: [
          { name: 'BUNA_PUBLIC_ORIGIN', value: frontendOrigin }
          { name: 'BUNA_PUBLIC_HOSTS', value: '${appName}.${environment.properties.defaultDomain},${appName}.internal.${environment.properties.defaultDomain},localhost,127.0.0.1' }
        ]
        probes: [{
          type: 'Readiness'
          httpGet: {
            path: '/health'
            port: 8080
            httpHeaders: [{ name: 'Host', value: 'localhost' }]
          }
          initialDelaySeconds: 10
          periodSeconds: 10
        }]
      }]
      scale: {
        minReplicas: minimumReplicas
        maxReplicas: 1
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '10' } } }]
      }
    }
  }
  tags: { application: 'paper-overlap', purpose: 'public-mvp' }
}
output fqdn string = application.properties.configuration.ingress.fqdn
