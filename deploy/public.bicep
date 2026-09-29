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
@description('Empty only for legacy anonymous preview. All three team fields must be supplied together.')
param teamTenantId string = ''
param teamClientId string = ''
param teamOwnerObjectId string = ''
@allowed(['', 'anonymous', 'team', 'email-gate'])
param accessMode string = ''
@secure()
@description('One email or comma-separated allowed emails; requires a multi-email-compatible image for lists.')
param allowedEmail string = ''
param attestedCorpusSha string = ''
param sharedAccountUrl string = ''
param sharedContainer string = ''
param sharedIdentityClientId string = ''

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
      secrets: empty(allowedEmail) ? [] : [{ name: 'allowed-email', value: allowedEmail }]
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
          { name: 'BUNA_TEAM_TENANT', value: teamTenantId }
          { name: 'BUNA_TEAM_CLIENT', value: teamClientId }
          { name: 'BUNA_TEAM_OWNER_OID', value: teamOwnerObjectId }
          { name: 'BUNA_ACCESS_MODE', value: accessMode }
          { name: 'BUNA_ATTESTED_CORPUS_SHA', value: attestedCorpusSha }
          { name: 'BUNA_SHARED_ACCOUNT_URL', value: sharedAccountUrl }
          { name: 'BUNA_SHARED_CONTAINER', value: sharedContainer }
          { name: 'BUNA_SHARED_IDENTITY_CLIENT_ID', value: sharedIdentityClientId }
          {
            name: 'BUNA_ALLOWED_EMAIL'
            ...(!empty(allowedEmail) ? { secretRef: 'allowed-email' } : { value: '' })
          }
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
